import torch

from torchdynamo import eval_frame
from torchdynamo.bytecode_transformation import create_instruction
from torchdynamo.bytecode_transformation import debug_checks
from torchdynamo.bytecode_transformation import transform_code_object
from torchdynamo.guards import GuardedCode
from torchdynamo.symbolic_convert import convert_frame_assert


def same(a, b):
    """Check correctness to see if a and b match"""
    if isinstance(a, (list, tuple)):
        assert isinstance(b, (list, tuple))
        return all(same(ai, bi) for ai, bi in zip(a, b))
    elif isinstance(a, torch.Tensor):
        assert isinstance(b, torch.Tensor)
        return torch.allclose(a, b, atol=1e-4, rtol=1e-4)
    elif type(a).__name__ == "SquashedNormal":
        return same(a.mean, b.mean)
    else:
        raise RuntimeError(f"unsupported type: {type(a).__name__}")


def debug_insert_nops(frame):
    """used to debug jump updates"""

    def insert_nops(instructions, code_options):
        instructions.insert(0, create_instruction("NOP"))
        instructions.insert(0, create_instruction("NOP"))

    debug_checks(frame.f_code)
    return GuardedCode(transform_code_object(frame.f_code, insert_nops))


class CompileCounter:
    def __init__(self):
        self.frame_count = 0
        self.op_count = 0

    def __call__(self, gm: torch.fx.GraphModule):
        self.frame_count += 1
        for node in gm.graph.nodes:
            if "call" in node.op:
                self.op_count += 1
        return gm.forward


def standard_test(self, fn, nargs, expected_ops=None):
    actual = CompileCounter()
    if expected_ops is None:
        expected = CompileCounter()
        gm = torch.fx.symbolic_trace(fn)
        expected(gm)
        gm.graph.print_tabular()
        expected_ops = expected.op_count
    args1 = [torch.randn(10, 10) for _ in range(nargs)]
    args2 = [torch.randn(10, 10) for _ in range(nargs)]
    correct1 = fn(*args1)
    correct2 = fn(*args2)
    with eval_frame.optimize(convert_frame_assert(actual)):
        val1a = fn(*args1)
        val2a = fn(*args2)
        val1b = fn(*args1)
        val2b = fn(*args2)
    self.assertTrue(same(val1a, correct1))
    self.assertTrue(same(val1b, correct1))
    self.assertTrue(same(val2a, correct2))
    self.assertTrue(same(val2b, correct2))
    self.assertEqual(actual.frame_count, 1)
    self.assertEqual(actual.op_count, expected_ops)
