#!/usr/bin/env python
import argparse
import collections
import copy
import csv
import functools
import gc
import getpass
import io
import logging
import os
import re
import sys
import textwrap
import time
import warnings
from os.path import abspath
from os.path import exists

import numpy as np
import torch
from scipy.stats import gmean
from scipy.stats import ttest_ind

from torchdynamo import symbolic_convert
from torchdynamo.optimizations.backends import optimize_for_inference, onnxrt
from torchdynamo.optimizations.inference import user_compiler
from torchdynamo.profiler import fx_insert_profiling
from torchdynamo.profiler import ProfileMetrics
from torchdynamo.profiler import Profiler
from torchdynamo.testing import dummy_fx_compile
from torchdynamo.testing import format_speedup
from torchdynamo.testing import same
import torchdynamo

os.environ["KALDI_ROOT"] = "/tmp"  # avoids some spam
torchbench_dir = abspath(
    "../torchbench" if exists("../torchbench") else "../torchbenchmark"
)
assert os.path.exists(torchbench_dir)
os.chdir(torchbench_dir)
sys.path.append(torchbench_dir)
log = logging.getLogger(__name__)
SKIP = {
    # torchbench `get_model()` is broken these:
    "albert",
    "demucs",
    "hf_T5",
    "hf_Reformer",
    "hf_Longformer",
    "hf_GPT2",
    "hf_DistilBert",
    "hf_BigBird",
    "hf_Bert",
    "hf_Bart",
    "nvidia_deeprecommender",
    "hf_Albert",
}
current_name = ""
current_device = ""

if getpass.getuser() == "jansel":
    # jansel applied this fix https://github.com/pytorch/benchmark/pull/479
    SKIP.clear()


class NullContext:
    def __enter__(self):
        pass

    def __exit__(self, exc_type, exc_val, exc_tb):
        pass


def synchronize():
    pass


def short_name(name, limit=20):
    """Truncate a model name to limit chars"""
    return name if len(name) <= limit else f"{name[:limit - 3].rstrip('_')}..."


def iter_models(args):
    # Disable auto-scripting in demucs
    torch.jit.ScriptModule = torch.nn.Module
    from fastNLP.core import logger

    logger.setLevel(logging.WARNING)
    from torchbenchmark import list_models  # noqa

    for benchmark_cls in list_models():
        if (
            not re.search("|".join(args.filter), benchmark_cls.name, re.I)
            or re.search("|".join(args.exclude), benchmark_cls.name, re.I)
            or benchmark_cls.name in SKIP
        ):
            continue
        for device in args.devices:
            try:
                benchmark = benchmark_cls(device=device, jit=False)
                model, example_inputs = benchmark.get_module()
                model.eval()
                gc.collect()
                global current_name, current_device
                current_device = device
                current_name = short_name(benchmark.name)
                yield device, current_name, model, example_inputs
            except NotImplementedError:
                pass
            except Exception:
                log.exception(f"misconfigured model {benchmark_cls.name}")


def timed(model, example_inputs, times=1):
    synchronize()
    gc.collect()
    torch.manual_seed(1337)
    t0 = time.perf_counter()
    for _ in range(times):
        result = model(*example_inputs)
        synchronize()
    t1 = time.perf_counter()
    return result, t1 - t0


class Stats:
    totals = collections.defaultdict(collections.Counter)

    @classmethod
    def reset_counters(cls):
        for k, v in symbolic_convert.counters.items():
            cls.totals[k].update(v)
        ok = symbolic_convert.counters["frames"]["ok"]
        total = symbolic_convert.counters["frames"]["total"]
        symbolic_convert.counters.clear()
        return ok, total

    @classmethod
    def print_summary(cls):
        for k, v in sorted(cls.totals.items()):
            lines = "\n  ".join(map(str, v.most_common(50)))
            print(f"STATS {k}\n  {lines}")


def coverage_experiment(coverage_results, model, example_inputs):
    profiler = Profiler()
    with profiler.prof, torchdynamo.run():
        model(*example_inputs)
    coverage_result = profiler.results()
    coverage_results.append(coverage_result.percent())
    return coverage_result


def speedup_experiment(speedups, args, model, example_inputs):
    timings = np.zeros((args.repeat, 2), np.float64)
    for rep in range(args.repeat):
        # interleave the runs to handle frequency scaling and load changes
        _, timings[rep, 0] = timed(model, example_inputs)
        with torchdynamo.run():
            _, timings[rep, 1] = timed(model, example_inputs)
    pvalue = ttest_ind(timings[:, 0], timings[:, 1]).pvalue
    median = np.median(timings, axis=0)
    speedup = median[0] / median[1]
    speedups.append(speedup)
    output_csv(
        "speedups.csv",
        ("dev", "name", "speedup"),
    ).writerow([current_device, current_name, f"{speedup:.4f}"])
    return format_speedup(speedup, pvalue)


@functools.lru_cache(1)
def output_csv(name, headers):
    output = csv.writer(
        io.TextIOWrapper(
            open(os.path.join(torchdynamo.config.base_dir, name), "wb", buffering=0),
            "utf-8",
            write_through=True,
        )
    )
    output.writerow(headers)
    return output


def speedup_experiment2(speedups, args, model, example_inputs):
    try:
        ts = torch.jit.script(model)
    except Exception:
        ts = None

    try:
        ofi = optimize_for_inference(torch.jit.script(model), example_inputs)
    except Exception:
        ofi = None

    try:
        ort = onnxrt(torch.jit.script(model), example_inputs)
    except Exception:
        ort = None

    timings = np.zeros((args.repeat, 5), np.float64)
    timings.fill(1.0e10)
    for rep in range(args.repeat):
        # interleave the runs to handle frequency scaling and load changes
        _, timings[rep, 0] = timed(model, example_inputs)
        if ts is not None:
            _, timings[rep, 1] = timed(ts, example_inputs)
        if ofi is not None:
            _, timings[rep, 2] = timed(ofi, example_inputs)
        if ort is not None:
            _, timings[rep, 3] = timed(ort, example_inputs)
        with torchdynamo.run():
            _, timings[rep, 4] = timed(model, example_inputs)

    pvalue = [
        ttest_ind(timings[:, 0], timings[:, i]).pvalue
        for i in range(1, timings.shape[1])
    ]
    median = np.median(timings, axis=0)
    speedup = median[0] / median[1:]
    if ts is None:
        speedup[0] = 0.0
    if ort is None:
        speedup[1] = 0.0
    speedups.append(speedup)
    result = " ".join(
        [
            format_speedup(s, p, m is not None)
            for s, p, m in zip(speedup, pvalue, [ts, ofi, ort, model])
        ]
    )
    output_csv(
        "baselines.csv",
        ("dev", "name", "ts", "optimize_for_inference", "onnxrt", "torchdynamo"),
    ).writerow([current_device, current_name] + [f"{x:.4f}" for x in speedup])
    return result


def null_experiment(model, example_inputs):
    return []


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--filter", "-k", action="append", help="filter benchmarks")
    parser.add_argument("--exclude", "-x", action="append", help="filter benchmarks")
    parser.add_argument("--devices", "-d", action="append", help="cpu or cuda")
    parser.add_argument(
        "--repeat", "-n", type=int, default=30, help="number of timing runs"
    )
    parser.add_argument("--threads", "-t", type=int, help="number of threads to use")
    parser.add_argument("--verbose", "-v", action="store_true", help="show errors")
    parser.add_argument(
        "--no-skip", action="store_true", help="run models that don't fx cleanly"
    )
    parser.add_argument("--overhead", action="store_true", help="measure overheads")
    parser.add_argument(
        "--speedup", action="store_true", help="measure speedup with default passes"
    )
    parser.add_argument(
        "--speedup2",
        action="store_true",
    )
    parser.add_argument(
        "--nothing", action="store_true", help="just check the benchmark works"
    )
    parser.add_argument(
        "--nops", action="store_true", help="check bytecode rewriting works"
    )
    parser.add_argument("--minimum-call-count", type=int)
    args = parser.parse_args()

    # defaults
    args.devices = args.devices or ["cpu"]
    args.filter = args.filter or [r"."]
    args.exclude = args.exclude or [r"^$"]

    if args.devices != ["cpu"] and torch.cuda.is_available():
        global synchronize
        synchronize = torch.cuda.synchronize

    if args.no_skip:
        SKIP.clear()

    torch._C._jit_override_can_fuse_on_cpu(True)

    if args.threads:
        torch.set_num_threads(args.threads)

    if args.verbose:
        torchdynamo.config.debug = True

    coverage_results = []
    speedups = []
    experiment = null_experiment

    if args.overhead:
        optimize_ctx = torchdynamo.optimize(dummy_fx_compile)
        experiment = functools.partial(speedup_experiment, speedups, args)
    elif args.speedup:
        optimize_ctx = torchdynamo.optimize(user_compiler)
        experiment = functools.partial(speedup_experiment, speedups, args)
    elif args.speedup2:
        optimize_ctx = torchdynamo.optimize(user_compiler)
        experiment = functools.partial(speedup_experiment2, speedups, args)
    elif args.nothing:
        optimize_ctx = NullContext()
    elif args.nops:
        optimize_ctx = torchdynamo.eval_frame.optimize(
            torchdynamo.testing.debug_insert_nops
        )
    else:
        optimize_ctx = torchdynamo.optimize(fx_insert_profiling)
        experiment = functools.partial(coverage_experiment, coverage_results)

    if args.minimum_call_count:
        torchdynamo.config.minimum_call_count = args.minimum_call_count

    for device, name, model, example_inputs in iter_models(args):
        sys.stdout.write(f"{current_device:4} {current_name:20} ")
        sys.stdout.flush()
        torch.manual_seed(1337)
        correct_result = copy.deepcopy(model)(*example_inputs)
        torch.manual_seed(1337)
        torchdynamo.reset()
        with optimize_ctx:
            new_result = model(*example_inputs)
        if not same(correct_result, new_result):
            print("INCORRECT")
            continue
        ok, total = Stats.reset_counters()
        results = []

        # run one more time to see if we reached a fixed point
        with optimize_ctx:
            model(*example_inputs)
        _, frames_second_pass = Stats.reset_counters()  # should be 0
        results.append(f"{ok:3}/{total:3} frames (+{frames_second_pass:2}),")

        results.append(experiment(model, example_inputs))
        print(" ".join(map(str, results)))

    Stats.print_summary()
    if coverage_results:
        print(
            "\nMEAN COVERAGE:",
            functools.reduce(ProfileMetrics.__add__, coverage_results)
            / len(coverage_results),
        )
    if speedups:
        print(
            textwrap.dedent(
                f"""
                MEAN SPEEDUP {np.mean(speedups, axis=0)}
                GEOMEAN SPEEDUP {gmean(speedups, axis=0)}"""
            )
        )


if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING)
    warnings.filterwarnings("ignore")
    main()
