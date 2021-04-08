# Copyright (c) Facebook, Inc. and its affiliates.
#
# This source code is licensed under the BSD license found in the
# LICENSE file in the root directory of this source tree.

"""
Testing MultiProcessPipe Module
"""

import functools
import tempfile
from typing import Any, List

import pytest
import torch
import torch.distributed.autograd as dist_autograd
from torch.distributed.optim import DistributedOptimizer
import torch.distributed.rpc as rpc
import torch.multiprocessing as mp
import torch.nn as nn

from fairscale.experimental.nn.multiprocess_pipe import (
    DistributedLoss,
    DistributedPipeline,
    PipelineModule,
    PipelineModulesGraph,
)
from fairscale.utils.testing import torch_version

CPU_DEVICES = ["worker0/cpu", "worker1/cpu"]
GPU_DEVICES = ["worker0/cuda:0", "worker1/cuda:1"]
if torch.cuda.is_available():
    DEVICES = [CPU_DEVICES, GPU_DEVICES]
else:
    DEVICES = [CPU_DEVICES]


pytestmark = pytest.mark.skipif(torch_version() < (1, 9, 0), reason="requires torch version >= 1.9.0")


def rpc_worker(rank, world_size, init_file, func, *args):
    options = rpc.TensorPipeRpcBackendOptions(init_method="file://" + init_file)
    for i in range(world_size):
        options.set_device_map("worker" + str(i), {rank: i})
    rpc.init_rpc(
        "worker" + str(rank),
        rank=rank,
        world_size=world_size,
        backend=rpc.BackendType.TENSORPIPE,
        rpc_backend_options=options,
    )
    if rank == 0:
        func(*args)
    rpc.shutdown()


def create_sequence_pipeline(
    layers: List[PipelineModule], balance: List[int], devices: List[str], **kwargs: Any
) -> DistributedPipeline:
    """A simple helper function to create a pipeline from list of pipeline-modules that run sequentially.
       Args:
           layers: list of modules. They should not be already assigned a remote-device.
           balance: a list of integers how layers should be paritioned. Sum of numbers in 'balance'
               should be equal to the number of layers.
           devices: specification of remote device for each partition. Should be of the same length
               as 'balance'.
    """
    index = 0
    for num_layers, remote_device in zip(balance, devices):
        next_index = index + num_layers
        for li in range(index, next_index):
            layers[li].instantiate(remote_device)
        index = next_index

    graph = PipelineModulesGraph()
    graph.add_sequence(layers)
    graph.feed_model_input(layers[0])

    return DistributedPipeline(graph, **kwargs)


def rpc_test(world_size=1):
    def decorator(func):
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            mp.spawn(rpc_worker, args=(world_size, tempfile.mkstemp()[1], func, *kwargs.values()), nprocs=world_size)

        globals()["test_" + func.__name__] = wrapper
        return func

    return decorator


@rpc_test()
@pytest.mark.parametrize("devices", DEVICES)
def create(devices):
    model = [PipelineModule(nn.Linear, (4, 4), {})]
    pipe = create_sequence_pipeline(model, balance=[1], chunks=1, devices=devices[:1])


@rpc_test()
def create_multiple_layers():
    model = [PipelineModule(nn.Linear, (4, 4), {}), PipelineModule(nn.ReLU, (), {})]
    pipe = create_sequence_pipeline(model, balance=[1, 1], chunks=1, devices=["worker0/cpu", "worker0/cpu"])


@rpc_test(world_size=2)
@pytest.mark.parametrize("devices", DEVICES)
def create_multiple_workers(devices):
    model = [PipelineModule(nn.Linear, (4, 4), {}), PipelineModule(nn.ReLU, (), {})]
    pipe = create_sequence_pipeline(model, balance=[1, 1], chunks=1, devices=devices[:2])


@rpc_test(world_size=2)
@pytest.mark.parametrize("devices", DEVICES)
def parameter_rrefs(devices):
    model = [PipelineModule(nn.Linear, (4, 4), {}), PipelineModule(nn.ReLU, (), {})]
    pipe = create_sequence_pipeline(model, balance=[1, 1], chunks=1, devices=devices[:2])
    parameter_rrefs = pipe.parameter_rrefs()
    assert len(parameter_rrefs) == 2


@rpc_test(world_size=1)
@pytest.mark.parametrize("devices", DEVICES)
def forward(devices):
    yh = torch.tensor([1.0, 0.0])
    x = torch.tensor([1.0, -1.0])
    model = [PipelineModule(nn.ReLU, (), {})]
    pipe = create_sequence_pipeline(model, balance=[1], chunks=1, devices=devices[:1])
    y = pipe(x).to_here().cpu()
    assert torch.equal(y, yh), f"{y} != {yh}"


@rpc_test(world_size=1)
@pytest.mark.parametrize("devices", DEVICES)
def forward_chunks(devices):
    yh = torch.tensor([1.0, 0.0, 2.0, 0.0, 3.0, 0.0, 4.0, 0.0])
    x = torch.tensor([1.0, -1.0, 2.0, -2.0, 3.0, -3.0, 4.0, -4.0])
    model = [PipelineModule(nn.ReLU, (), {})]
    pipe = create_sequence_pipeline(model, balance=[1], chunks=4, devices=devices[:1])
    y = pipe(x).to_here().cpu()
    assert torch.equal(y, yh), f"{y} != {yh}"


@rpc_test(world_size=2)
@pytest.mark.parametrize("devices", DEVICES)
@pytest.mark.parametrize("checkpoint", ["never", "always", "except_last"])
def forward_multi(devices, checkpoint):
    device = devices[0].split("/")[1]
    torch.random.manual_seed(3)
    torch.cuda.manual_seed_all(3)
    x = torch.randn(8, 4).to(device)
    x.requires_grad = True  # TODO(msb) remove this limitation
    model = [PipelineModule(nn.Linear, (4, 4), {}), PipelineModule(nn.ReLU, (), {})]
    pipe = create_sequence_pipeline(model, balance=[1, 1], chunks=4, devices=devices[:2], checkpoint=checkpoint)
    y = pipe(x).to_here()
    expected_sum = torch.tensor(5.0615)
    assert y.shape == torch.Size([8, 4])
    assert y.requires_grad is True
    assert torch.allclose(y.sum(), expected_sum), f"{y.sum()} != {expected_sum}"


@rpc_test(world_size=2)
@pytest.mark.parametrize("devices", DEVICES)
def backward(devices):
    device = devices[0].split("/")[1]
    torch.random.manual_seed(3)
    criterion = DistributedLoss(torch.nn.MSELoss)
    x = torch.randn(8, 4).to(device)
    model = [PipelineModule(nn.Linear, (4, 4), {}), PipelineModule(nn.ReLU, (), {})]
    pipe = create_sequence_pipeline(model, balance=[1, 1], chunks=4, devices=devices[:2])
    with dist_autograd.context() as context_id:
        y = pipe(x)
        loss = criterion(y, rpc.RRef(x))
        loss.backward(context_id)
        grads = dist_autograd.get_gradients(context_id)
    assert len(grads) == 2


@rpc_test(world_size=2)
@pytest.mark.parametrize("devices", DEVICES)
def update(devices):
    device = devices[0].split("/")[1]
    torch.random.manual_seed(3)
    criterion = DistributedLoss(torch.nn.MSELoss)
    x = torch.randn(8, 4).to(device)
    model = [PipelineModule(nn.Linear, (4, 4), {}), PipelineModule(nn.ReLU, (), {})]
    pipe = create_sequence_pipeline(model, balance=[1, 1], chunks=4, devices=devices[:2])
    params = pipe.parameter_rrefs()
    opt = DistributedOptimizer(torch.optim.SGD, pipe.parameter_rrefs(), lr=0.05,)
    losses = []
    for i in range(2):
        with dist_autograd.context() as context_id:
            y = pipe(x)
            loss = criterion(y, rpc.RRef(x))
            losses.append(loss)
            loss.backward(context_id)
            opt.step(context_id)
    losses = [l.to_here() for l in losses]
    assert losses[0] > losses[1], f"{losses[0]} !> {losses[1]}"


class ConcatenateTensors(nn.Module):
    def forward(self, *inputs):
        return torch.cat(inputs, dim=1)


class SplitTensors(nn.Module):
    def forward(self, input):
        return torch.split(input, (input.shape[1] + 1) // 2, dim=1)


@rpc_test(world_size=2)
@pytest.mark.parametrize("devices", DEVICES)
def multi_input_multi_output_layers(devices):
    device = devices[0].split("/")[1]
    torch.random.manual_seed(3)
    criterion = DistributedLoss(torch.nn.MSELoss)
    x = torch.randn(8, 4).to(device)

    #                                / ->linear_layer_2_1
    # input -> linear_layer1 -> split                     ->concatenate->linear_layer_3
    #                                \ ->linear_layer_2_2

    linear_layer_1 = PipelineModule(nn.Linear, (4, 4), {}, remote_device=devices[0])
    split = PipelineModule(SplitTensors, (), {}, num_outputs=2, remote_device=devices[0])
    linear_layers_2 = [
        PipelineModule(nn.Linear, (2, 2), {}, remote_device=devices[0]),
        PipelineModule(nn.Linear, (2, 2), {}, remote_device=devices[1]),
    ]
    concatenate = PipelineModule(ConcatenateTensors, tuple(), num_inputs=2, remote_device=devices[1])

    graph = PipelineModulesGraph()
    graph.add_sequence([linear_layer_1, split])
    graph.feed_model_input(linear_layer_1)
    graph.fan_out(split, linear_layers_2)
    graph.add_multi_input_layer(concatenate, linear_layers_2)

    pipe = DistributedPipeline(graph, chunks=4)
    params = pipe.parameter_rrefs()
    opt = DistributedOptimizer(torch.optim.SGD, pipe.parameter_rrefs(), lr=0.05,)
    losses = []
    for i in range(2):
        with dist_autograd.context() as context_id:
            y = pipe(x)
            loss = criterion(y, rpc.RRef(x))
            losses.append(loss)
            loss.backward(context_id)
            opt.step(context_id)
    losses = [l.to_here() for l in losses]
    assert losses[0] > losses[1], f"{losses[0]} !> {losses[1]}"
