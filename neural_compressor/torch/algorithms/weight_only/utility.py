# Copyright (c) 2024 Intel Corporation
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import math

import torch

from neural_compressor.torch.utils import logger

__all__ = [
    "FLOAT_MAPPING",
    "FP4_BNB",
    "FP4_BNB_BIT",
    "FP4_E2M1",
    "FP4_E2M1_BIT",
    "GraphTrace",
    "INT_MAPPING",
    "NF4",
    "NF4_BIT",
    "calibration",
    "fetch_module",
    "forward_wrapper",
    "get_absorb_layers",
    "get_block_prefix",
    "get_example_input",
    "get_hidden_states",
    "get_module",
    "get_module_input_output",
    "get_parent",
    "model_forward",
    "move_input_to_device",
    "qdq_weight_actor",
    "qdq_weight_asym",
    "qdq_weight_sym",
    "quant_tensor",
    "quant_weight_w_scale",
    "quantize_4bit",
    "search_clip",
    "set_module",
]

NF4 = [
    -1.0,
    -0.6961928009986877,
    -0.5250730514526367,
    -0.39491748809814453,
    -0.28444138169288635,
    -0.18477343022823334,
    -0.09105003625154495,
    0.0,
    0.07958029955625534,
    0.16093020141124725,
    0.24611230194568634,
    0.33791524171829224,
    0.44070982933044434,
    0.5626170039176941,
    0.7229568362236023,
    1.0,
]
FP4_BNB = [-12.0, -8.0, -6.0, -4.0, -3.0, -2.0, -0.0625, 0, 0.0625, 2.0, 3.0, 4.0, 6.0, 8.0, 12.0]
FP4_E2M1 = [-6.0, -4.0, -3.0, -2.0, -1.5, -1.0, -0.0625, 0, 0.0625, 1.0, 1.5, 2.0, 3.0, 4.0, 6.0]

# the order is the same as float list, bit value range is [-7, 7]
# 1111 = -1, 1110 = -2, 1101= -3, ...

NF4_BIT = [7, 1, 2, 3, 4, 5, 6, 0, -8, -7, -6, -5, -4, -3, -2, -1]
FP4_BNB_BIT = [-5, -6, -3, -4, -1, -2, -7, 0, 1, 6, 7, 4, 5, 2, 3]
FP4_E2M1_BIT = [-1, -2, -3, -4, -5, -6, -7, 0, 1, 2, 3, 4, 5, 6, 7]

FLOAT_MAPPING = {"nf4": NF4, "fp4": FP4_BNB, "fp4_e2m1_bnb": FP4_BNB, "fp4_e2m1": FP4_E2M1}
INT_MAPPING = {"nf4": NF4_BIT, "fp4": FP4_BNB_BIT, "fp4_e2m1_bnb": FP4_BNB_BIT, "fp4_e2m1": FP4_E2M1_BIT}


def quantize_4bit(tensor, quantile=1.0, dtype="nf4", return_int=False, **kwargs):
    """Quantize tensor to NF4/FP4 data type.

    Args:
        tensor: input tensor
        quantile (float, optional): percentile of clip. Defaults to 1.0.
        dtype (str, optional): data type. Defaults to 'nf4'.
        return_int (bool, optional): whether return int data. Defaults to False.

    Returns:
        q_tensor: fake quantized tensor
    """
    assert dtype in FLOAT_MAPPING, "unexpected data type."
    allow_data = FLOAT_MAPPING[dtype]
    allow_data_bit = INT_MAPPING[dtype]
    # preprocess dtype
    dtype_copy = False
    if tensor.dtype != torch.float32:
        dtype_copy = True
        org_tensor = tensor  # record id to copy_()
        tensor = org_tensor.type(torch.float32)
    # get scale and update tensor
    if "scale" in kwargs:
        scale = kwargs["scale"]
    else:
        scale = tensor.abs().max(1)[0] * quantile / max(allow_data)
        scale.unsqueeze_(dim=-1)
    tensor.div_(scale)
    mid_data = [(allow_data[i] + allow_data[i + 1]) / 2 for i in range(len(allow_data) - 1)]
    q_tensor = torch.zeros_like(tensor)
    for i in range(len(allow_data)):
        data = allow_data_bit[i] if return_int or "cast_int" in kwargs else allow_data[i]
        if i == 0:
            q_tensor += torch.where(tensor <= mid_data[i], data, 0)
        elif i == len(allow_data) - 1:
            q_tensor += torch.where(tensor > mid_data[i - 1], data, 0)
        else:
            q_tensor += torch.where((mid_data[i - 1] < tensor) & (tensor <= mid_data[i]), data, 0)
    tensor.copy_(q_tensor)
    keep_scale = kwargs.get("double_quant", False)
    if return_int or keep_scale:
        return tensor, scale, None
    tensor.mul_(scale)
    # postprocess dtype
    if dtype_copy:
        org_tensor.copy_(tensor.type(org_tensor.dtype))
        return org_tensor
    return tensor


def qdq_weight_asym(weight, bits=4, quantile=1.0, return_int=False, **kwargs):
    """Quant and dequant tensor with asym schema.

    Args:
        weight:  input weight
        bits (int, optional): bits. Defaults to 4.
        quantile (float, optional): percentile of clip. Defaults to 1.0.
        return_int (bool, optional): Choose return fp32 or int8/uint8 data.
                                     Defaults to False.

    Returns:
        output: qdq weight
    """
    # preprocess dtype
    dtype_copy = False
    if weight.dtype != torch.float32:
        dtype_copy = True
        org_weight = weight  # record id to copy_()
        weight = org_weight.type(torch.float32)
    maxq = torch.tensor(2**bits - 1)
    zeros = torch.zeros(weight.shape[0], device=weight.device)
    wmin = torch.minimum(weight.min(1)[0], zeros)
    wmax = torch.maximum(weight.max(1)[0], zeros)
    wmin = wmin * quantile
    wmax = wmax * quantile
    tmp = (wmin == 0) & (wmax == 0)
    wmin[tmp] = -1
    wmax[tmp] = +1
    scale = (wmax - wmin) / maxq
    zp = torch.round(-wmin / scale)
    scale.unsqueeze_(dim=-1)
    zp.unsqueeze_(dim=-1)
    weight.div_(scale)
    weight.round_()
    weight.add_(zp)
    weight.clamp_(0, maxq)
    keep_scale = kwargs.get("double_quant", False)
    if return_int or keep_scale:
        return weight, scale, zp
    weight.sub_(zp)
    weight.mul_(scale)
    # postprocess dtype
    if dtype_copy:
        org_weight.copy_(weight.type(org_weight.dtype))
        return org_weight
    return weight


def qdq_weight_sym(weight, bits=4, quantile=1.0, return_int=False, full_range=False, **kwargs):
    """Quant and dequant tensor with sym schema.

    Args:
        weight : input weight
        bits (int, optional): bits. Defaults to 4.
        quantile (float, optional): percentile of clip. Defaults to 1.0.
        return_int (bool, optional): Choose return fp32 or int8/uint8 data.
                                     Defaults to False.
        full_range (bool, optional): Choose sym range whether use -2**(bits-1).
                For example: 4 bit
                    scale = amax / 8 if full_range else amax / 7
                    If True, scale = -scale if abs(min)> abs(max) else scale
                    Defaults to False.

    Returns:
        output: qdq weight
    """
    # preprocess dtype
    dtype_copy = False
    if weight.dtype != torch.float32:
        dtype_copy = True
        org_weight = weight  # record id to copy_()
        weight = org_weight.type(torch.float32)
    # assert bits > 1, "symmetric scheme only supports bits > 1"
    maxq = torch.tensor(2 ** (bits - 1) - 1).to(weight.device)
    minq = torch.tensor(-(2 ** (bits - 1))).to(weight.device)
    if bits == 1:  # pragma: no cover
        maxq = torch.tensor(2 ** (bits - 1))
        minq = torch.tensor(2 ** (bits - 1) - 1)
    max_val = torch.max(weight, 1)[0]
    min_val = torch.min(weight, 1)[0]
    flip_flag = torch.abs(max_val) > torch.abs(min_val)
    wmax = torch.max(torch.abs(max_val), torch.abs(min_val))
    wmax = wmax * quantile
    tmp = wmax == 0
    wmax[tmp] = +1
    if full_range:
        # use -8, 8 to make sure amax is not changed after fake quant
        scale = wmax / (-minq)
        tmp = scale * flip_flag.int()
        scale -= 2 * tmp  # set negative scale with flip_flag
    else:
        scale = wmax / maxq
    scale.unsqueeze_(dim=-1)
    weight.div_(scale)
    weight.round_()
    weight.clamp_(minq, maxq)
    keep_scale = kwargs.get("double_quant", False)
    if return_int or keep_scale:
        return weight, scale, None
    weight.mul_(scale)
    # postprocess dtype
    if dtype_copy:
        org_weight.copy_(weight.type(org_weight.dtype))
        return org_weight
    return weight


def qdq_weight_actor(weight, bits, scheme, quantile=1.0, dtype="int", return_int=False, full_range=False, **kwargs):
    """Quant and dequant tensor per channel. It is an in-place op.

    Args:
        weight : input weight
        bits (int, optional): bits. Defaults to 4.
        quantile (float, optional): percentile of clip. Defaults to 1.0.
        dtype (str, optional): select from int, nf4, fp4. Defaults to int.
        return_int (bool, optional): Choose return fp32 or int8/uint8 data.
                                     Defaults to False.
        full_range (bool, optional): Choose sym range whether use -2**(bits-1).

    Returns:
        output: qdq weight
    """
    assert bits > 0, "bits should be larger than 0"

    if dtype in FLOAT_MAPPING.keys():
        return quantize_4bit(weight, quantile=quantile, dtype=dtype, return_int=return_int, **kwargs)
    if scheme == "sym":
        return qdq_weight_sym(weight, bits, quantile, return_int, full_range, **kwargs)
    else:
        return qdq_weight_asym(weight, bits, quantile, return_int, **kwargs)


def quant_tensor(
    weight,
    bits=4,
    group_size=-1,
    scheme="asym",
    quantile=1.0,
    dtype="int",
    return_int=False,
    full_range=False,
    **kwargs,
):
    """Quant and dequant tensor with group size. It's an in-place function.

    Args:
        weight: input weight
        bits (int, optional): bits. Defaults to 4.
        group_size (int, optional): how many elements share one scale/zp. Defaults to -1.
        scheme (str, optional): sym or asym. Defaults to "asym".
        quantile (float, optional): percentile of clip. Defaults to 1.0.
        dtype (str, optional): select from int, nf4, fp4. Defaults to int.
        return_int (bool, optional): Choose return fp32 or int8/uint8 data.
                                     Defaults to False.
        full_range (bool, optional): Choose sym range whether use -2**(bits-1).

    Returns:
        output: qdq weight.
    """
    quant_scale = kwargs.get("double_quant", False)
    if bits <= 0:  # pragma: no cover
        return weight
    # case 1, group size = -1
    if group_size == -1 or weight.shape[1] < group_size:
        group_size = weight.shape[1]
    # case 2, reshape based on group size
    orig_shape = weight.shape
    if weight.shape[1] % group_size == 0:
        weight = weight.reshape(-1, group_size)
        weight = qdq_weight_actor(
            weight,
            bits,
            scheme=scheme,
            quantile=quantile,
            return_int=return_int,
            full_range=full_range,
            dtype=dtype,
            **kwargs,
        )
        if return_int or quant_scale:
            weight, scale, zp = weight
            weight = weight.reshape(orig_shape)
            scale = scale.reshape(orig_shape[0], -1)
            if zp is not None:
                zp = zp.reshape(orig_shape[0], -1)
            q_state = weight, scale, zp
        else:
            return weight.reshape(orig_shape)
    else:
        # case 3, process left part split by group size
        split_index = weight.shape[1] // group_size * group_size
        weight1 = weight[:, :split_index]
        weight1 = weight1.reshape(-1, group_size)
        weight1 = qdq_weight_actor(
            weight1,
            bits,
            scheme=scheme,
            quantile=quantile,
            return_int=return_int,
            full_range=full_range,
            dtype=dtype,
            **kwargs,
        )
        if return_int or quant_scale:
            weight1, scale1, zp1 = weight1
            scale1 = scale1.reshape(orig_shape[0], -1)
            if zp1 is not None:
                zp1 = zp1.reshape(orig_shape[0], -1)
        weight1 = weight1.reshape(orig_shape[0], split_index)
        weight2 = weight[:, split_index:]
        weight2 = qdq_weight_actor(
            weight2,
            bits,
            scheme=scheme,
            dtype=dtype,
            quantile=quantile,
            return_int=return_int,
            full_range=full_range,
            **kwargs,
        )
        if return_int or quant_scale:
            weight2, scale2, zp2 = weight2
            weight.copy_(torch.cat([weight1, weight2], dim=1))
            scale = torch.cat([scale1, scale2], dim=1)
            zp = None if zp2 is None else torch.cat([zp1, zp2], dim=1)
            q_state = (weight, scale, zp)
        else:
            weight.copy_(torch.cat([weight1, weight2], dim=1))
            return weight
    if quant_scale:
        weight, scale, zp = q_state
        scale_dtype = kwargs.get("double_quant_dtype", "int")
        scale_bits = kwargs.get("double_quant_bits", 8)
        scale_scheme = kwargs.get("double_quant_scheme", "asym")
        scale_group_size = kwargs.get("double_quant_group_size", 256)
        scale_return_int = kwargs.get("double_quant_return_int", return_int)
        orig_scale_shape = scale.shape
        scale = scale.reshape(1, -1)
        # pre-process: scale_mean
        if scale_scheme == "asym":
            scale_mean = scale.mean()
            scale.sub_(scale_mean)
            scale_scheme = "sym"
        # process: scale
        scale = quant_tensor(
            scale,
            dtype=scale_dtype,
            bits=scale_bits,
            group_size=scale_group_size,
            scheme=scale_scheme,
            quantile=1.0,
            return_int=scale_return_int,
            full_range=False,
        )
        # post-process: scale_mean
        if scale_return_int:
            scale, hyper_scale, hyper_zp = scale
            scale = scale.reshape(orig_scale_shape)
            scale = (scale, hyper_scale, scale_mean)
        else:
            if kwargs.get("double_quant_scheme", "sym") == "asym":
                scale.add_(scale_mean)
            scale = scale.reshape(orig_scale_shape)
        # post-process: weight * scale
        if return_int:
            return weight, scale, zp
        else:
            if weight.shape[1] % group_size != 0:
                if zp is not None:
                    weight1 = weight1.reshape(-1, group_size).sub_(zp[:, :-1].reshape(-1, 1))
                    weight2 = weight2.sub_(zp[:, -1].reshape(-1, 1))
                else:
                    weight1 = weight1.reshape(-1, group_size)
                weight1 = weight1.mul_(scale[:, :-1].reshape(-1, 1))
                weight1 = weight1.reshape(orig_shape[0], -1)
                weight2 = weight2.mul_(scale[:, -1].reshape(-1, 1))
                weight.copy_(torch.cat([weight1, weight2], dim=1))
            else:
                if zp is not None:
                    weight = weight.reshape(-1, group_size) - zp.reshape(-1, 1)
                else:
                    weight = weight.reshape(-1, group_size)
                weight = weight.mul_(scale.reshape(-1, 1))
                weight = weight.reshape(orig_shape[0], -1)
            return weight
    else:
        return q_state


def search_clip(m, bits=4, group_size=32, scheme="asym", dtype="int", enable_full_range=False):
    """Search best clip range of each linear in current block. It's not an in-place function.

    Args:
        m (torch.nn.Module): torch module.
        bits (int, optional): num bits.
        group_size (int, optional): how many elements share one scale/zp.
        scheme (str, optional): sym or asym.
        dtype (str, optional): select from int, nf4, fp4. Defaults to int.
        enable_full_range (bool, optional): Choose sym range whether use -2**(bits-1).

    Returns:
        best_clip_ratio (float): best percentile of clip
    """
    org_weight = m.weight.data.clone()
    logger.debug("Searching the best clip range with RTN algorithm")
    best_error = float("inf")
    best_clip_ratio = None
    n_grid = 200
    max_shrink = 0.2
    history = []
    for i_s in range(int(max_shrink * n_grid)):
        ratio = 1 - i_s / n_grid  # 1, 0.805-1.0
        quant_tensor(
            m.weight.data,  # in-place mode
            dtype=dtype,
            bits=bits,
            group_size=group_size,
            scheme=scheme,
            full_range=enable_full_range,
            quantile=ratio,
        )
        loss = (org_weight - m.weight.data).float().pow(2).mean().item()
        m.weight.data.copy_(org_weight)
        history.append(loss)
        is_best = loss < best_error
        if is_best:
            best_error = loss
            best_clip_ratio = ratio
    logger.debug("The loss history of different clip range:{}".format(history))
    logger.debug("The best clip ratio is {}".format(best_clip_ratio))
    return best_clip_ratio


def quant_weight_w_scale(weight, scale, zp=None, group_size=-1, dtype="int"):
    """Quant and dequant tensor with group size. It's an in-place function.

    Args:
        weight: input weight
        scale: scale
        zp: zero point
        group_size (int, optional): how many elements share one scale/zp. Defaults to -1.
        dtype: data type, for NF4 FP4

    Returns:
        output: int weight.
    """
    device = weight.device
    scale = scale.to(device)
    if zp is not None:
        zp = zp.to(device)
    # group_size = -1
    if group_size == -1:
        if dtype in FLOAT_MAPPING.keys():  # NF4 FP4
            return quantize_4bit(weight, scale=scale, dtype=dtype, return_int=True)[0]
        return weight.div_(scale).round_() if zp is None else weight.div_(scale).add_(zp).round_()
    int_weight = torch.zeros(weight.shape).to(device)
    leng = weight.shape[1] // group_size
    tail_flag = False if weight.shape[1] % group_size == 0 else True
    # group_size != -1
    for i in range(leng):
        if dtype in FLOAT_MAPPING.keys():  # NF4 FP4
            int_weight_tmp = weight[:, i * group_size : (i + 1) * group_size]
            quantize_4bit(int_weight_tmp, scale=scale[:, i].unsqueeze(1), dtype=dtype, return_int=True)[0]
        else:
            int_weight_tmp = weight[:, i * group_size : (i + 1) * group_size].div_(scale[:, i].unsqueeze(1))
            if zp is not None:
                int_weight_tmp.add_(zp[:, i].unsqueeze(1))
            int_weight[:, i * group_size : (i + 1) * group_size].copy_(int_weight_tmp.round_())
    # tail_flag
    if tail_flag:
        if dtype in FLOAT_MAPPING.keys():  # NF4 FP4
            int_weight_tmp = weight[:, leng * group_size :]
            quantize_4bit(int_weight_tmp, scale=scale[:, -1].unsqueeze(1), dtype=dtype, return_int=True)[0]
        else:
            int_weight_tmp = weight[:, leng * group_size :].div_(scale[:, -1].unsqueeze(1))
            if zp is not None:
                int_weight_tmp.add_(zp[:, -1].unsqueeze(1))
            int_weight[:, leng * group_size :].copy_(int_weight_tmp.round_())
    return int_weight


# -------------- AWQ ---------------------------
from collections import UserDict
from functools import partial


# AWQ Required, copy from neural_compressor/adaptor/torch_utils/smooth_quant.py
def model_forward(model, dataloader, iters, device):
    try:
        cnt = 0
        for idx, (input, label) in enumerate(dataloader):
            output = forward_wrapper(model, input, device)
            cnt += 1
            if iters != -1 and cnt >= iters:
                break
    except Exception as e:
        cnt = 0
        for idx, input in enumerate(dataloader):
            output = forward_wrapper(model, input, device)
            cnt += 1
            if iters != -1 and cnt >= iters:
                break


# copy from neural_compressor/adaptor/torch_utils/smooth_quant.py
# TODO: potential bug, data type
def forward_wrapper(model, input, device=torch.device("cpu")):
    try:
        model = model.to(device)
        input = move_input_to_device(input, device)
    except Exception as e:
        logger.warning(e)
        logger.warning("Please check the input device if the error raised.")
    if isinstance(input, dict) or isinstance(input, UserDict):
        output = model(**input)
    elif isinstance(input, list) or isinstance(input, tuple):
        try:
            output = model(*input)
        except:
            output = model(input)
    else:
        output = model(input)
    return output


# copy from neural_compressor/adaptor/torch_utils/smooth_quant.py
def move_input_to_device(input, device=torch.device("cpu")):
    if isinstance(input, dict) or isinstance(input, UserDict):
        tmp_input = {}
        for k, inp in input.items():
            tmp_input[k] = move_input_to_device(inp, device)
        input = tmp_input
    elif isinstance(input, list) or isinstance(input, tuple):
        is_tuple = isinstance(input, tuple)
        tmp_input = []
        for inp in input:
            tmp_input.append(move_input_to_device(inp, device))
        input = tuple(tmp_input) if is_tuple else tmp_input
    elif isinstance(input, torch.Tensor):
        input = input.to(device)  # pylint: disable=no-member
    return input


# copy from neural_compressor/adaptor/torch_utils/smooth_quant.py
def set_module(model, key, new_module):
    """Set new module into model by key name.

    Args:
        model (torch.nn.Module): original model
        key (str): module name to be replaced
        new_module (torch.nn.Module): new module to be inserted
    """
    module = model
    name_list = key.split(".")
    for name in name_list[:-1]:
        if hasattr(module, name):
            module = getattr(module, name)
        elif hasattr(module, ("sq_linear")):  # for peft models that Linears are contained in Linear
            module = getattr(module, "sq_linear")
            module = getattr(module, name)
        elif hasattr(module, ("orig_layer")):  # for peft models and auto alpha
            module = getattr(module, "orig_layer")
            module = getattr(module, name)
        else:
            module = module

    if hasattr(module, "sq_linear") and name_list[-1] != "sq_linear":  # for peft models
        module = getattr(module, "sq_linear")
    if hasattr(module, "orig_layer") and name_list[-1] != "orig_layer":  # for peft models and auto alpha
        module = getattr(module, "orig_layer")
    setattr(module, name_list[-1], new_module)


# copy from neural_compressor/adaptor/torch_utils/util.py
def fetch_module(model, op_name):
    """Get module with a given op name.

    Args:
        model (object): the input model.
        op_name (str): name of op.

    Returns:
        module (object).
    """
    module = model
    name_list = op_name.split(".")
    for name in name_list:
        if hasattr(module, name):
            module = getattr(module, name)
        else:
            module = module
    return module


# copy from neural_compressor/adaptor/torch_utils/util.py
def get_absorb_layers(model, example_inputs, supported_layers=["Linear"], folding=False):
    """Get absorb_to_layer and no_absorb_layer.

    Args:
        model (torch.nn.Module): input model
        example_inputs: example_inputs
        supported_layers (list, optional): supported_layers. Defaults to ['Linear'].
        folding (bool, optional): whether allow self-absorption. Defaults to False.

    Returns:
        absorb_to_layer: dict of absorb_to_layer. eg. {absorb, [absorbed_1, xx]}
        no_absorb_layers: list of no_absorb_layers
    """
    # get modules that can be absorbed.
    # from .smooth_quant import GraphTrace, move GraphTrace into this file

    tg = GraphTrace()
    absorb_to_layer, no_absorb_layers = tg.get_absorb_to_layer(model, example_inputs, supported_layers)
    if absorb_to_layer is None or absorb_to_layer == {}:
        absorb_to_layer = {}
        logger.warning("No absorb layer is detected.")
        # if no_absorb_layers is None, jit trace failed.
        # collect all linears for next step
        if no_absorb_layers is None:
            no_absorb_layers = []
            op_types = ["Linear"]
            for name, module in model.named_modules():
                for op_type in op_types:
                    if op_type == str(module.__class__.__name__):
                        no_absorb_layers.append(name)
    return absorb_to_layer, no_absorb_layers


# copy from neural_compressor/adaptor/torch_utils/smooth_quant.py
def get_parent(node, all_parents=False):
    if node.inputs() is None:
        return None
    elif len(list(node.inputs())) == 0:
        return None
    if not all_parents:
        return list(node.inputs())[0].node()
    else:
        return list(node.inputs())


# copy from neural_compressor/adaptor/torch_utils/smooth_quant.py
def get_module(model, key):
    """Get module from model by key name.

    Args:
        model (torch.nn.Module): original model
        key (str): module name to be replaced
    """
    module = model
    name_list = key.split(".")
    for name in name_list:
        if hasattr(module, name):
            module = getattr(module, name)
        elif hasattr(module, "sq_linear"):  # for peft models
            module = getattr(module, "sq_linear")
            module = getattr(module, name)
        elif hasattr(module, "orig_layer"):  # for peft models and auto alpha
            module = getattr(module, "orig_layer")
            module = getattr(module, name)
        else:
            module = module
    return module


# copy from neural_compressor/adaptor/torch_utils/smooth_quant.py
class GraphTrace:
    """"""

    def __init__(self):
        self.supported_torch_module_to_aten = {
            "Linear": "aten::linear",
            "Conv2d": "aten::_convolution",
            "ConvTranspose2d": "aten::_convolution",
            "LayerNorm": "aten::layer_norm",
            "BatchNorm2d": "aten::batch_norm",
            "GroupNorm": "aten::group_norm",
            "InstanceNorm2d": "aten::instance_norm",
            "LlamaRMSNorm": "aten::mul",
            "T5LayerNorm": "aten::mul",
            "LPLayerNorm": "aten::layer_norm",  ##mpt_chat
        }

        ##TODO potential bug, need to check only have one bug
        ##TODO, must satisfy af(x)=f(ax),current skip layer may be incomplete
        self.skip_ops_to_find_absorb = ["aten::to", "aten::relu", "aten::leaky_relu", "aten::hardtanh"]

        self.could_absorb_layers = [
            "aten::layer_norm",
            "aten::batch_norm",
            "aten::linear",
            "aten::_convolution",
            "aten::group_norm",
            "aten::instance_norm",
            "aten::mul",
        ]  ##TODO,support more norm

    def trace(self, model, dummy_input):
        traced_model = None
        optimize_numerics = False
        orig_device = str(next(model.parameters()).device)
        if orig_device != "cpu" and orig_device != "meta":  # pragma: no cover
            model = model.to("cpu")
            dummy_input = move_input_to_device(dummy_input, "cpu")
        if isinstance(dummy_input, dict) or isinstance(dummy_input, UserDict):
            try:
                # pylint: disable=E1123, E1120
                traced_model = torch.jit.trace(
                    model, example_kwarg_inputs=dict(dummy_input), strict=False, check_trace=False
                )
                traced_model = torch.jit.freeze(traced_model.eval(), optimize_numerics=optimize_numerics)
            except Exception as e:
                logger.warning(e)
                logger.warning("Jit trace in GraphTrace failed, absorb layer detection is skipped")
        else:
            try:
                traced_model = torch.jit.trace(model, dummy_input, strict=False)
                traced_model = torch.jit.freeze(traced_model.eval(), optimize_numerics=optimize_numerics)
            except:
                try:
                    traced_model = torch.jit.trace(model, dummy_input[0], strict=False)
                    traced_model = torch.jit.freeze(traced_model.eval(), optimize_numerics=optimize_numerics)
                except Exception as e:
                    logger.warning(e)
                    logger.warning("Jit trace in GraphTrace failed, absorb layer detection is skipped")
        model = model.to(orig_device)
        return traced_model

    def get_nodes(self, traced_model, op_types=["Linear"]):
        if isinstance(op_types, str):
            op_types = [op_types]
        nodes = []
        for node in traced_model.graph.nodes():
            node_type = node.kind()
            for op_type in op_types:
                if node_type == op_type:
                    nodes.append((node, op_type))
                    break
        return nodes

    def get_prev_absorb_layer(self, nodes):
        prev_absorb_layer = []
        for node in nodes:
            parent = get_parent(node)
            while 1:
                if parent.kind() in self.skip_ops_to_find_absorb:
                    parent = get_parent(parent)
                    continue
                if parent.kind() in self.could_absorb_layers:
                    parent_out_kinds = []
                    for val_user in list(parent.outputs())[0].uses():
                        next_node = val_user.user
                        parent_out_kinds.append(next_node.kind())
                    parent_out_kinds = set(parent_out_kinds)
                    parent_out_kinds.discard("aten::size")

                    if parent_out_kinds == parent_out_kinds.intersection(self.could_absorb_layers):
                        prev_absorb_layer.append(parent)
                    elif parent_out_kinds.intersection(self.skip_ops_to_find_absorb):
                        res = self.skip_op_absorb_helper(parent)
                        prev_absorb_layer.append(parent) if res else prev_absorb_layer.append(None)
                    else:  # When parent to multiple ops, sq transformation could be wrong.
                        prev_absorb_layer.append(None)
                else:
                    prev_absorb_layer.append(None)
                break
        return prev_absorb_layer

    def skip_op_absorb_helper(self, parent_node):
        for val_user in list(parent_node.outputs())[0].uses():
            next_node = val_user.user
            if next_node.kind() == "aten::size":
                continue
            elif next_node.kind() in self.could_absorb_layers:
                continue
            elif next_node.kind() in self.skip_ops_to_find_absorb:
                node_res = self.skip_op_absorb_helper(next_node)
                if not node_res:
                    return False
            else:
                return False
        return True

    def mapping_torch_module_to_aten(self, op_types):
        res = []
        for op in op_types:
            if op not in self.supported_torch_module_to_aten.keys():
                logger.warning(f"{op} is not supported in smooth quant, ignoring...")
                continue
            res.append(self.supported_torch_module_to_aten[op])
        res = list(set(res))
        return res

    def _check_valid_conv(self, module):
        """Remove group conv except depthwise conv
        :param module:

        :return:
        """
        if not isinstance(module, torch.nn.Conv2d):
            return True
        if module.groups > 1:
            if module.in_channels == module.out_channels and module.groups == module.in_channels:
                return True
            else:
                return False
        return True

    def get_absorb_to_layer(self, model, example_input, op_types, skip_unsupported_layers=True):
        traced_model = self.trace(model, example_input)
        if traced_model is None:
            return None, None

        aten_op_types = self.mapping_torch_module_to_aten(op_types)
        nodes_types = self.get_nodes(traced_model, aten_op_types)
        nodes = [node_type[0] for node_type in nodes_types]
        nodes_prev_absorb = self.get_prev_absorb_layer(nodes)
        absorb_to_layer = {}
        no_absorb_layers = []
        for index, absorb in enumerate(nodes_prev_absorb):
            if absorb is None:
                no_absorb_layers.append(".".join(nodes[index].scopeName().split("/")[-1].split(".")[1:]))
                continue
            node = nodes[index]
            layer_name = ".".join(node.scopeName().split("/")[-1].split(".")[1:])
            absorb_name = ".".join(absorb.scopeName().split("/")[-1].split(".")[1:])
            if layer_name == "" or absorb_name == "":
                continue
            if absorb_name in absorb_to_layer.keys():
                absorb_to_layer[absorb_name].append(layer_name)
            else:
                absorb_to_layer[absorb_name] = [layer_name]
        if skip_unsupported_layers:
            absorb_to_layer = self.remove_unsupported_layers(model, absorb_to_layer, no_absorb_layers)
        return absorb_to_layer, no_absorb_layers

    def remove_unsupported_layers(self, model, absorb_to_layer, no_absorb_layers):
        res = {}
        for key in absorb_to_layer.keys():
            absorb_layer = get_module(model, key)
            layer_type = absorb_layer.__class__.__name__
            if layer_type not in self.supported_torch_module_to_aten.keys():
                no_absorb_layers.extend(absorb_to_layer[key])
                continue
            supported = True
            for layer_name in absorb_to_layer[key]:
                layer = get_module(model, layer_name)
                layer_type = layer.__class__.__name__
                if (layer_type not in self.supported_torch_module_to_aten.keys()) or not self._check_valid_conv(layer):
                    supported = False
                    no_absorb_layers.extend(absorb_to_layer[key])
                    break
            if supported:
                res[key] = absorb_to_layer[key]
        return res


# copy from neural_compressor/adaptor/torch_utils/util.py
def get_block_prefix(model):
    """Get prefix and number of blocks.

    Args:
        model (torch.nn.Module): input model

    Returns:
        block_prefix(str): block_list name in model
        block_num(int): number of block in block_list
    """
    module_types = [torch.nn.ModuleList]
    for n, m in model.named_modules():
        if type(m) in module_types:
            block_prefix = n
            block_num = len(m)
            logger.debug(f"block_prefix: {block_prefix}, block_num: {block_num} ")
            break
    assert block_num > 0, "block num shouldn't be zero!"
    return block_prefix, block_num


# copy from neural_compressor/adaptor/torch_utils/util.py
def get_example_input(dataloader, i=1):
    """Get the example input.

    Args:
        dataloader (object): calibration dataset.

    Returns:
        example_inp (object).
    """
    iter = 0
    try:
        for example_inp, label in dataloader:
            if iter == i:
                break
            else:
                iter += 1
    except:
        for example_inp in dataloader:
            if iter == i:
                break
            else:
                iter += 1
    return example_inp


# copy from neural_compressor/adaptor/torch_utils/util.py
def get_hidden_states(model, dataloader=None, n_samples=128, calib_func=None):
    """Get the input args and kwargs of first block.

    Args:
        model (torch.nn.Module): input model
        dataloader (dataloader, optional): input dataloader. Defaults to None.
        n_samples (int, optional): number samples from dataloader. Defaults to 128.
        calib_func (func, optional): a calib func to replace dataloader. Defaults to None.

    Raises:
        ValueError: to avoid inference of rest parts in model

    Returns:
        total_block_args(list): a list of input args of each batch
        total_block_kwargs(list):  a list of input kwargs of each batch
    """
    # Step 1: replace block_forward to collect block inputs and avoid entire inference
    total_block_args = []
    total_block_kwargs = []

    def forward(layer, *args, **kwargs):
        # update total_hidden_states, total_block_kwargs, per batch
        total_block_args.append(list(args))
        total_block_kwargs.append(kwargs)
        raise ValueError

    block_prefix, block_num = get_block_prefix(model)
    block_list = fetch_module(model, block_prefix)
    first_block = block_list[0]
    block_forward_cache = first_block.forward
    first_block.forward = partial(forward, first_block)

    # Step 2: replace model_forward to avoid ValueError
    model_forward_cache = model.forward

    def model_forward(model, *args, **kwargs):
        nonlocal model_forward_cache
        try:
            model_forward_cache(*args, **kwargs)
        except ValueError:
            pass

    model.forward = partial(model_forward, model)

    # Step 3: execute calibration
    calibration(model, dataloader=dataloader, n_samples=n_samples, calib_func=calib_func)
    logger.info("The hidden_states collection is done.")

    # Step 4: recover model and block forward
    model.forward = model_forward_cache
    first_block.forward = block_forward_cache
    return total_block_args, total_block_kwargs


# copy from neural_compressor/adaptor/torch_utils/util.py
def calibration(model, dataloader=None, n_samples=128, calib_func=None):
    """Calibration with dataloader or calib_func.

    Args:
        model (torch.nn.Module): input model
        dataloader: dataloader. Defaults to None.
        n_samples (int, optional): n_samples. Defaults to 128.
        calib_func: calib_func. Defaults to None.
    """
    # calibration with dataloader or calib_func
    if calib_func is not None:
        calib_func(model)
    else:
        # from .smooth_quant import model_forward, move into this file

        batch_size = dataloader.batch_size
        iters = int(math.ceil(n_samples / batch_size))
        if n_samples % batch_size != 0:
            logger.info(
                "calibration samples increase from {} to {} due to batch_size is {}".format(
                    n_samples,
                    iters * batch_size,
                    batch_size,
                )
            )
        model_forward(model, dataloader, iters, next(model.parameters()).device)


# copy from neural_compressor/adaptor/torch_utils/util.py
def get_module_input_output(
    model, module_hook_config={}, dataloader=None, iters=-1, calib_func=None, input_func=None, output_func=None
):
    """A help function to get input and output tensor of modules in module_name_list.

    Args:
        model: torch model.
        module_hook_config (dict, optional): required module name for input/output. Defaults to {}.
            For example:
                module_hook_config = {
                    'fc1': ['output'],
                    'fc2': ['input', 'output']
                }
        dataloader: dataloader for model input.
        iters: iterations for inference.
        calib_func: a custom inference function to replace dataloader and iters.
        input_func: preprocess input for less memory usage
        output_func: preprocess output for less memory usage

    Returns:
        total_values: recorded input_values, output_values.
            for example:
                {'fc1':
                    {'input': [], 'output': []},
                }
    """
    from collections import defaultdict

    total_values = defaultdict(defaultdict)

    def _save_input_output_hook(name, record_input=False, record_output=False):
        """
        A forward hook to save input and output values of a module
            param name: the module name
            return: A hook function
        """

        def _hook(module, inputs, outputs):
            if record_input:
                input = inputs[0]
                if input_func is not None:
                    input = input_func(input)
                if name in total_values and "input" in total_values[name]:
                    total_values[name]["input"].append(input)
                else:
                    total_values[name]["input"] = [input]
            if record_output:
                output = outputs[0] if isinstance(outputs, tuple) else outputs
                if output_func is not None:
                    output = output_func(output)
                if input_func is not None:
                    input = input_func(input)
                if name in total_values and "output" in total_values[name]:
                    total_values[name]["output"].append(output)
                else:
                    total_values[name]["output"] = [output]

        return _hook

    hook_list = []
    for name, module in model.named_modules():
        if name in module_hook_config:
            require_list = module_hook_config[name]
            logger.debug(f"required hooks {name}: {require_list}")
            _hook = _save_input_output_hook(
                name,
                record_input="input" in require_list,
                record_output="output" in require_list,
            )
            require_list = module_hook_config[name]
            hook_list.append(module.register_forward_hook(_hook))
    if calib_func:
        calib_func(model)
    else:
        # from .smooth_quant import model_forward, move into this file

        model_forward(model, dataloader, iters, device=next(model.parameters()).device)
    for h in hook_list:
        h.remove()
    return total_values
