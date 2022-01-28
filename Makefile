.PHONY: default develop test torchbench format lint setup clean autotune

PY_FILES := $(wildcard *.py) $(wildcard torchdynamo/*.py) $(wildcard torchdynamo/*/*.py) $(wildcard tests/*.py)
C_FILES := $(wildcard torchdynamo/*.c torchdynamo/*.cpp)

default: develop

develop:
	python setup.py develop

test: develop
	pytest -v

torchbench: develop
	python torchbench.py

overhead: develop
	python torchbench.py --overhead

format:
	isort $(PY_FILES)
	black $(PY_FILES)
	clang-format-10 -i $(C_FILES)

lint:
	flake8 $(PY_FILES)
	clang-tidy-10 $(C_FILES)  -- \
		-I$(shell python -c "from distutils.sysconfig import get_python_inc as X; print(X())") \
		$(shell python -c 'from torch.utils.cpp_extension import include_paths; print(" ".join(map("-I{}".format, include_paths())))')

setup:
	pip install flake8 black "isort>=5.10.1" pytest ninja tabulate onnxruntime-gpu tensorflow-gpu onnx-tf

clean:
	python setup.py clean
	rm -rf build torchdynamo.egg-info torchdynamo/*.so

clone-deps:
	(cd .. \
		&& (test -e pytorch || git clone --recursive https://github.com/pytorch/pytorch pytorch) \
		&& (test -e functorch || git clone --recursive https://github.com/pytorch/functorch) \
		&& (test -e torchvision || git clone --recursive https://github.com/pytorch/vision torchvision) \
		&& (test -e torchtext || git clone --recursive https://github.com/pytorch/text torchtext) \
		&& (test -e torchaudio || git clone --recursive https://github.com/pytorch/audio torchaudio) \
		&& (test -e detectron2 || git clone --recursive https://github.com/facebookresearch/detectron2) \
		&& (test -e torchbenchmark || git clone --recursive https://github.com/jansel/benchmark torchbenchmark) \
	)

pull-deps:
	(cd ../pytorch        && git pull && git submodule update --init --recursive)
	(cd ../functorch      && git pull && git submodule update --init --recursive)
	(cd ../torchvision    && git pull && git submodule update --init --recursive)
	(cd ../torchtext      && git pull && git submodule update --init --recursive)
	(cd ../torchaudio     && git pull && git submodule update --init --recursive)
	(cd ../detectron2     && git pull && git submodule update --init --recursive)
	(cd ../torchbenchmark && git pull && git submodule update --init --recursive)

build-deps: clone-deps
	# conda create --prefix `pwd`/env python=3.8
	# conda activate `pwd`/env
	conda install -y astunparse numpy ninja pyyaml mkl mkl-include setuptools cmake cffi typing_extensions future six requests dataclasses
	conda install -y -c pytorch magma-cuda113
	make setup
	(cd ../pytorch     && python setup.py clean && env LDFLAGS="-lncurses" python setup.py develop)
	(cd ../functorch   && python setup.py clean && python setup.py develop)
	(cd ../torchvision && python setup.py clean && python setup.py develop)
	(cd ../torchtext   && python setup.py clean && python setup.py develop)
	(cd ../torchaudio  && python setup.py clean && python setup.py develop)
	(cd ../detectron2  && python setup.py clean && python setup.py develop)
	(cd ../torchbenchmark && python install.py)

autotune-cpu: develop
	rm -rf subgraphs
	python torchbench.py --speedup -n3
	python autotune.py
	python torchbench.py --speedup -n50

autotune-gpu: develop
	rm -rf subgraphs
	python torchbench.py --speedup -dcuda -n3
	python autotune.py
	python torchbench.py --speedup -dcuda -n100

autotune-gpu-nvfuser: develop
	rm -rf subgraphs
	python torchbench.py --speedup -dcuda --nvfuser -n3
	python autotune.py --nvfuser
	python torchbench.py --speedup -dcuda --nvfuser -n100

baseline-cpu: develop
	 rm -f baseline_*.csv
	 python torchbench.py -n50 --overhead
	 python torchbench.py -n50 --speedup-ts
	 python torchbench.py -n50 --speedup-sr
	 python torchbench.py -n50 --speedup-onnx
	 paste -d, baseline_ts.csv baseline_sr.csv baseline_onnx.csv > baseline_all.csv

baseline-gpu: develop
	 rm -f baseline_*.csv
	 python torchbench.py -dcuda -n100 --overhead
	 python torchbench.py -dcuda -n100 --speedup-ts && mv baseline_ts.csv baseline_nnc.csv
	 python torchbench.py -dcuda -n100 --speedup-ts --nvfuser && mv baseline_ts.csv baseline_nvfuser.csv
	 python torchbench.py -dcuda -n100 --speedup-trt
	 python torchbench.py -dcuda -n100 --speedup-onnx
	 paste -d, baseline_nnc.csv baseline_nvfuser.csv baseline_trt.csv baseline_onnx.csv > baseline_all.csv
