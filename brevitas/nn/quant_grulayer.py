# Copyright (c) 2019-     Xilinx, Inc              (Giuseppe Franco)
# Copyright (c) 2016-     Facebook, Inc            (Adam Paszke)
# Copyright (c) 2014-     Facebook, Inc            (Soumith Chintala)
# Copyright (c) 2011-2014 Idiap Research Institute (Ronan Collobert)
# Copyright (c) 2012-2014 Deepmind Technologies    (Koray Kavukcuoglu)
# Copyright (c) 2011-2012 NEC Laboratories America (Koray Kavukcuoglu)
# Copyright (c) 2011-2013 NYU                      (Clement Farabet)
# Copyright (c) 2006-2010 NEC Laboratories America (Ronan Collobert, Leon Bottou, Iain Melvin, Jason Weston)
# Copyright (c) 2006      Idiap Research Institute (Samy Bengio)
# Copyright (c) 2001-2004 Idiap Research Institute (Ronan Collobert, Samy Bengio, Johnny Mariethoz)
#
# From Caffe2:
#
# Copyright (c) 2016-present, Facebook Inc. All rights reserved.
#
# All contributions by Facebook:
# Copyright (c) 2016 Facebook Inc.
#
# All contributions by Google:
# Copyright (c) 2015 Google Inc.
# All rights reserved.
#
# All contributions by Yangqing Jia:
# Copyright (c) 2015 Yangqing Jia
# All rights reserved.
#
# All contributions from Caffe:
# Copyright(c) 2013, 2014, 2015, the respective contributors
# All rights reserved.
#
# All other contributions:
# Copyright(c) 2015, 2016 the respective contributors
# All rights reserved.
#
# Caffe2 uses a copyright model similar to Caffe: each contributor holds
# copyright over their contributions to Caffe2. The project versioning records
# all such contribution and copyright details. If a contributor wants to further
# mark their specific copyright on a particular contribution, they should
# indicate their copyright solely in the commit message of the change when it is
# committed.
#
# All rights reserved.
#
# Redistribution and use in source and binary forms, with or without
# modification, are permitted provided that the following conditions are met:
#
# 1. Redistributions of source code must retain the above copyright
# notice, this list of conditions and the following disclaimer.
#
# 2. Redistributions in binary form must reproduce the above copyright
# notice, this list of conditions and the following disclaimer in the
# documentation and/or other materials provided with the distribution.
#
# 3. Neither the names of Facebook, Deepmind Technologies, NYU, NEC Laboratories America
# and IDIAP Research Institute nor the names of its contributors may be
# used to endorse or promote products derived from this software without
# specific prior written permission.
#
# THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS"
# AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE
# IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE
# ARE DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT OWNER OR CONTRIBUTORS BE
# LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR
# CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF
# SUBSTITUTE GOODS OR SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS
# INTERRUPTION) HOWEVER CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN
# CONTRACT, STRICT LIABILITY, OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE)
# ARISING IN ANY WAY OUT OF THE USE OF THIS SOFTWARE, EVEN IF ADVISED OF THE
# POSSIBILITY OF SUCH DAMAGE.

from brevitas.core.bit_width import BitWidthImplType
from brevitas.core.quant import QuantType
from brevitas.nn import QuantSigmoid, QuantTanh, QuantIdentity
import torch.nn as nn
from brevitas.core.scaling import ScalingImplType, SCALING_SCALAR_SHAPE
from brevitas.core.stats import StatsInputViewShapeImpl, StatsOp
from brevitas.proxy.parameter_quant import _weight_quant_init_impl
from brevitas.proxy.runtime_quant import _activation_quant_init_impl
from brevitas.core.restrict_val import RestrictValueType, FloatToIntImplType
from brevitas.quant_tensor import QuantTensor
from brevitas.nn.quant_layer import SCALING_MIN_VAL
import torch
from brevitas.core.norm import NormImplType

from typing import Tuple, List
from torch import Tensor
from brevitas.core import ZERO_HW_SENTINEL_NAME, ZERO_HW_SENTINEL_VALUE
from collections import OrderedDict
import math

OVER_BATCH_OVER_CHANNELS_SHAPE = (1, -1, 1, 1)

__all__ = ['QuantGRULayer', 'BidirGRULayer']


@torch.jit.script
def reverse(lst):
    # type: (List[Tensor]) -> List[Tensor]
    out = torch.jit.annotate(List[Tensor], [])
    # start = len(lst) - 1
    end = len(lst)
    step = -1
    index = len(lst) - 1

    for i in range(end):
        out += [lst[index]]
        index = index + step
    return out


class QuantGRULayer(torch.jit.ScriptModule):
    __constants__ = ['reverse_input', 'batch_first', 'hidden_size']

    def __init__(self, input_size, hidden_size, weight_config, activation_config, norm_scale_out_config,
                 norm_scale_hidden_config, batch_first=False,
                 reverse_input=False, compute_output_scale=False,
                 compute_output_bit_width=False, return_quant_tensor=False):

        super(QuantGRULayer, self).__init__()
        self.register_buffer(ZERO_HW_SENTINEL_NAME, torch.tensor(ZERO_HW_SENTINEL_VALUE))
        self.return_quant_tensor = return_quant_tensor
        self.weight_config = weight_config
        self.activation_config = activation_config
        self.norm_scale_out_config = norm_scale_out_config
        self.norm_scale_hidden_config = norm_scale_hidden_config

        weight_ri = nn.Parameter(torch.randn(input_size, hidden_size), requires_grad=True)
        weight_ci = nn.Parameter(torch.randn(input_size, hidden_size), requires_grad=True)
        weight_ni = nn.Parameter(torch.randn(input_size, hidden_size), requires_grad=True)

        weight_rh = nn.Parameter(torch.randn(hidden_size, hidden_size), requires_grad=True)
        weight_ch = nn.Parameter(torch.randn(hidden_size, hidden_size), requires_grad=True)
        weight_nh = nn.Parameter(torch.randn(hidden_size, hidden_size), requires_grad=True)

        self.weight_ri = weight_ri
        self.weight_ci = weight_ci
        self.weight_ni = weight_ni

        self.weight_rh = weight_rh
        self.weight_ch = weight_ch
        self.weight_nh = weight_nh

        self.bias_r = nn.Parameter(torch.zeros(hidden_size), requires_grad=True)
        self.bias_i = nn.Parameter(torch.zeros(hidden_size), requires_grad=True)
        self.bias_ni = nn.Parameter(torch.zeros(hidden_size), requires_grad=True)
        self.bias_nh = nn.Parameter(torch.zeros(hidden_size), requires_grad=True)

        self.reverse_input = reverse_input
        self.batch_first = batch_first
        self.hidden_size = hidden_size
        self.reset_parameters()

        weight_scaling_stats_op = self.weight_config.get('weight_stats_input_view_shape_impl', 'MAX')
        self.weight_config['weight_scaling_stats_input_concat_dim'] = 0
        if self.weight_config.get('weight_scaling_per_output_channel', False):
            self.weight_config['weight_stats_input_view_shape_impl'] = StatsInputViewShapeImpl.OVER_OUTPUT_CHANNELS
            self.weight_config['weight_scaling_shape'] = self.per_output_channel_broadcastable_shape()
            self.weight_config['weight_scaling_stats_reduce_dim'] = 0
        else:
            self.weight_config['weight_scaling_shape'] = SCALING_SCALAR_SHAPE
            self.weight_config['weight_stats_input_view_shape_impl'] = StatsInputViewShapeImpl.OVER_TENSOR
            self.weight_config['weight_scaling_stats_reduce_dim'] = None

        if weight_scaling_stats_op == StatsOp.MAX_AVE or \
                weight_scaling_stats_op == StatsOp.MAX_L2:
            self.weight_config['weight_stats_input_view_shape_impl'] = StatsInputViewShapeImpl.OVER_OUTPUT_CHANNELS
            self.weight_config['weight_scaling_stats_reduce_dim'] = 0

        self.weight_proxy_r = self.configure_weight([weight_ri, weight_rh], self.weight_config)
        self.weight_proxy_i = self.configure_weight([weight_ci, weight_ch], self.weight_config)
        self.weight_proxy_n = self.configure_weight([weight_ni, weight_nh], self.weight_config)

        self.quant_sigmoid_r = self.configure_activation(self.activation_config, QuantSigmoid)
        self.quant_sigmoid_c = self.configure_activation(self.activation_config, QuantSigmoid)
        self.quant_tanh = self.configure_activation(self.activation_config, QuantTanh)

        self.norm_scale_newgate = self.configure_activation(self.norm_scale_hidden_config, QuantIdentity)
        self.norm_scale_out = self.configure_activation(self.norm_scale_out_config, QuantIdentity)

        if self.weight_config.get('weight_quant_type', 'QuantType.FP') == 'QuantType.FP' and compute_output_bit_width:
            raise Exception("Computing output bit width requires enabling quantization")
        if self.weight_config.get('bias_quant_type', 'QuantType.FP') != 'QuantType.FP' and not (
                compute_output_scale and compute_output_bit_width):
            raise Exception("Quantizing bias requires to compute output scale and output bit width")

    def reset_parameters(self):
        stdv = 1.0 / math.sqrt(self.hidden_size)
        for weight in self.parameters():
            torch.nn.init.uniform_(weight, -stdv, stdv)

    def per_output_channel_broadcastable_shape(self):
        output_dim = 1
        per_channel_size = [1] * len(self.weight_ci.size())
        per_channel_size[output_dim] = self.hidden_size
        per_channel_size = tuple(per_channel_size)
        return per_channel_size

    @torch.jit.script_method
    def forward_cycle(self, inputs, state, quant_weight_ih, quant_weight_hh, quant_weight_ni, quant_weight_nh, zhws):
        # type: (List[Tensor], Tensor, Tensor, Tensor, Tensor, Tensor, Tensor) -> Tuple[List[Tensor], Tensor]

        end = len(inputs)
        step = 1
        index = 0
        if self.reverse_input:
            end = len(inputs)
            index = end - 1
            step = -1

        outputs = torch.jit.annotate(List[Tensor], [])
        state = self.norm_scale_out(state, zhws)[0]
        for i in range(end):
            input_quant = self.norm_scale_out(inputs[index], zhws)[0]
            output, state = self.forward_iteration(input_quant, state, quant_weight_ih, quant_weight_hh,
                                                   quant_weight_ni,
                                                   quant_weight_nh, zhws)
            index = index + step

            outputs += [output]

        return outputs, state

    @torch.jit.script_method
    def forward_iteration(self, input, state,
                          quant_weight_ih, quant_weight_hh, quant_weight_ni, quant_weight_nh, zhws):

        gates = (torch.mm(input, quant_weight_ih) + torch.mm(state, quant_weight_hh))
        rgate, cgate = gates.chunk(2, 1)

        gates_ni = torch.mm(input, quant_weight_ni) + self.bias_ni
        gates_nh = torch.mm(state, quant_weight_nh) + self.bias_nh

        rgate = rgate + self.bias_r
        cgate = cgate + self.bias_i

        rgate = self.quant_sigmoid_r(rgate, zhws)[0]
        cgate = self.quant_sigmoid_c(cgate, zhws)[0]
        gates_ni = self.norm_scale_newgate(gates_ni, zhws)[0] + \
                   self.norm_scale_newgate(rgate * gates_nh, zhws)[0]
        ngate = self.quant_tanh(gates_ni, zhws)[0]

        state = self.norm_scale_out(state, zhws)[0] - self.norm_scale_out(ngate, zhws)[0]
        hy = self.norm_scale_out(ngate, zhws)[0] + self.norm_scale_out(cgate * state, zhws)[0]

        return hy, hy

    def forward(self, inputs, state=None):
        zero_hw_sentinel = getattr(self, 'zero_hw_sentinel')

        # Inline unpack input
        if isinstance(inputs, QuantTensor):
            inputs = inputs
        else:
            inputs, input_scale, input_bit_width = inputs, None, None

        quant_weight_ri, quant_weight_ri_scale, quant_weight_ri_bit_width = self.weight_proxy_r(self.weight_ri,
                                                                                                zero_hw_sentinel)
        quant_weight_ci, quant_weight_ci_scale, quant_weight_ci_bit_width = self.weight_proxy_i(self.weight_ci,
                                                                                                zero_hw_sentinel)
        quant_weight_ni, quant_weight_ni_scale, quant_weight_ni_bit_width = self.weight_proxy_n(self.weight_ni,
                                                                                                zero_hw_sentinel)

        quant_weight_rh, quant_weight_rh_scale, quant_weight_rh_bit_width = self.weight_proxy_r(self.weight_rh,
                                                                                                zero_hw_sentinel)
        quant_weight_ch, quant_weight_ch_scale, quant_weight_ch_bit_width = self.weight_proxy_i(self.weight_ch,
                                                                                                zero_hw_sentinel)
        quant_weight_nh, quant_weight_nh_scale, quant_weight_nh_bit_width = self.weight_proxy_n(self.weight_nh,
                                                                                                zero_hw_sentinel)

        quant_weight_ih = torch.cat([quant_weight_ri, quant_weight_ci], dim=1)
        quant_weight_hh = torch.cat([quant_weight_rh, quant_weight_ch], dim=1)

        if self.batch_first:
            dim = 1
            inputs_unbinded = inputs.unbind(1)
        else:
            dim = 0
            inputs_unbinded = inputs.unbind(0)
        batch_size = inputs_unbinded[0].shape[0]

        if state is None:
            device = self.weight_ri.device
            state = torch.zeros(batch_size, self.hidden_size, device=device)

        outputs, state = self.forward_cycle(inputs_unbinded, state, quant_weight_ih, quant_weight_hh, quant_weight_ni,
                                            quant_weight_nh, zero_hw_sentinel)

        if self.reverse_input:
            return torch.stack(reverse(outputs), dim=dim), outputs[-1]
        else:
            return torch.stack(outputs, dim=dim), outputs[-1]

    def max_output_bit_width(self, input_bit_width, weight_bit_width):
        raise Exception("Not supported yet")

    def configure_weight(self, weight, weight_config):
        zero_hw_sentinel = getattr(self, 'zero_hw_sentinel')
        wqp = _weight_quant_init_impl(bit_width=weight_config.get('weight_bit_width', 8),
                                      quant_type=weight_config.get('weight_quant_type', 'FP'),
                                      narrow_range=weight_config.get('weight_narrow_range', True),
                                      norm_impl_type=weight_config.get('weight_norm_impl_type',
                                                                       NormImplType.SAME_AS_SCALING),
                                      scaling_override=weight_config.get('weight_scaling_override',
                                                                         None),
                                      restrict_scaling_type=weight_config.get(
                                          'weight_restrict_scaling_type', RestrictValueType.LOG_FP),
                                      scaling_const=weight_config.get('weight_scaling_const', None),
                                      scaling_stats_op=weight_config.get('weight_scaling_stats_op',
                                                                         StatsOp.MAX),
                                      scaling_impl_type=weight_config.get('weight_scaling_impl_type',
                                                                          ScalingImplType.STATS),
                                      scaling_stats_reduce_dim=weight_config.get(
                                          'weight_scaling_stats_reduce_dim', None),
                                      scaling_shape=weight_config.get('weight_scaling_shape',
                                                                      SCALING_SCALAR_SHAPE),
                                      bit_width_impl_type=weight_config.get('weight_bit_width_impl_type',
                                                                            BitWidthImplType.CONST),
                                      bit_width_impl_override=weight_config.get(
                                          'weight_bit_width_impl_override', None),
                                      restrict_bit_width_type=weight_config.get(
                                          'weight_restrict_bit_width_type', RestrictValueType.INT),
                                      min_overall_bit_width=weight_config.get(
                                          'weight_min_overall_bit_width', 2),
                                      max_overall_bit_width=weight_config.get(
                                          'weight_max_overall_bit_width', None),
                                      ternary_threshold=weight_config.get('weight_ternary_threshold',
                                                                          0.5),
                                      scaling_stats_input_view_shape_impl=weight_config.get(
                                          'weight_stats_input_view_shape_impl',
                                          StatsInputViewShapeImpl.OVER_TENSOR),
                                      scaling_stats_input_concat_dim=weight_config.get(
                                          'weight_scaling_stats_input_concat_dim', 0),
                                      scaling_stats_sigma=weight_config.get('weight_scaling_stats_sigma',
                                                                            3.0),
                                      scaling_min_val=weight_config.get('weight_scaling_min_val',
                                                                        SCALING_MIN_VAL),
                                      override_pretrained_bit_width=weight_config.get(
                                          'weight_override_pretrained_bit_width', False),
                                      tracked_parameter_list=weight,
                                      zero_hw_sentinel=zero_hw_sentinel)

        return wqp

    def configure_activation(self, activation_config, activation_func=QuantSigmoid):
        signed = True
        max_val = 1
        min_val = -1
        if activation_func == QuantTanh:
            activation_impl = nn.Tanh()
            activation_config['min_val'] = -1.0
            activation_config['max_val'] = 1.0
        elif activation_func == QuantSigmoid:
            activation_impl = nn.Sigmoid()
            activation_config['min_val'] = 0.0
            activation_config['max_val'] = 1.0
            signed = False
        else:
            activation_impl = nn.Identity()

        activation_object = _activation_quant_init_impl(activation_impl=activation_impl,
                                                        bit_width=activation_config.get('bit_width', 8),
                                                        narrow_range=activation_config.get('narrow_range', True),
                                                        quant_type=activation_config.get('quant_type', 'FP'),
                                                        float_to_int_impl_type=activation_config.get(
                                                            'float_to_int_impl_type', FloatToIntImplType.ROUND),
                                                        min_overall_bit_width=activation_config.get(
                                                            'min_overall_bit_width', 2),
                                                        max_overall_bit_width=activation_config.get(
                                                            'max_overall_bit_width', None),
                                                        bit_width_impl_override=activation_config.get(
                                                            'bit_width_impl_override', None),
                                                        bit_width_impl_type=activation_config.get('bit_width_impl_type',
                                                                                                  BitWidthImplType.CONST),
                                                        restrict_bit_width_type=activation_config.get(
                                                            'restrict_bit_width_type', RestrictValueType.INT),
                                                        restrict_scaling_type=activation_config.get(
                                                            'restrict_scaling_type', RestrictValueType.LOG_FP),
                                                        scaling_min_val=activation_config.get('scaling_min_val',
                                                                                              SCALING_MIN_VAL),
                                                        override_pretrained_bit_width=activation_config.get(
                                                            'override_pretrained_bit_width', False),
                                                        min_val=activation_config.get('min_val', min_val),
                                                        max_val=activation_config.get('max_val', max_val),
                                                        signed=activation_config.get('signed', signed),
                                                        per_channel_broadcastable_shape=activation_config.get(
                                                            'per_channel_broadcastable_shape',
                                                            None),
                                                        scaling_per_channel=activation_config.get('scaling_per_channel',
                                                                                                  False),
                                                        scaling_override=activation_config.get('scaling_override',
                                                                                               None),
                                                        scaling_impl_type=activation_config.get('scaling_impl_type',
                                                                                                ScalingImplType.CONST),
                                                        scaling_stats_sigma=activation_config.get('scaling_stats_sigma',
                                                                                                  2.0),
                                                        scaling_stats_op=activation_config.get('scaling_stats_op',
                                                                                               StatsOp.MAX),
                                                        scaling_stats_buffer_momentum=activation_config.get(
                                                            'scaling_stats_buffer_momentum',
                                                            0.1),
                                                        scaling_stats_permute_dims=activation_config.get(
                                                            'scaling_stats_permute_dims',
                                                            (1, 0, 2, 3)))

        return activation_object

    def _load_from_state_dict(self, state_dict, prefix, local_metadata, strict,
                              missing_keys, unexpected_keys, error_msgs):

        stats_key = 'tensor_quant.scaling_impl.runtime_stats.running_stats'
        activation_type = self.norm_scale_out_config.get('scaling_impl_type', ScalingImplType.CONST)
        name = '.'.join([prefix, 'norm_scale_out']) if prefix is not '' else 'norm_scale_out'
        name = '.'.join([name, stats_key])
        if (name in state_dict) and activation_type == ScalingImplType.PARAMETER:
            raise Exception("Switching from STATS to PARAMETER in activations is not supported")

        dict_to_change = dict()
        for k, v in state_dict.items():
            if k.startswith(prefix):
                dict_to_change[k] = v

        for k in list(state_dict.keys()):
            if k.startswith(prefix):
                del state_dict[k]

        dict_changed = self.fix_state_dict(prefix, dict_to_change)
        for k, v in dict_changed.items():
            state_dict[k] = v
        super(QuantGRULayer, self)._load_from_state_dict(state_dict, prefix, local_metadata, strict,
                                                         missing_keys, unexpected_keys, error_msgs)

        zero_hw_sentinel_key = prefix + "zero_hw_sentinel"

        if zero_hw_sentinel_key in missing_keys:
            missing_keys.remove(zero_hw_sentinel_key)
        if zero_hw_sentinel_key in unexpected_keys:  # for retrocompatibility with when it wasn't removed
            unexpected_keys.remove(zero_hw_sentinel_key)

    def fix_state_dict(self, prefix, state_dict):
        newstate = OrderedDict()
        hidden = self.hidden_size
        bias_r = torch.zeros(hidden)
        bias_i = torch.zeros(hidden)
        prefix_len = len(prefix)
        for name, value in state_dict.items():
            if name[:prefix_len + 7] == prefix + 'bias_ih':
                bias_r = bias_r + value[:hidden]
                bias_i = bias_i + value[hidden:hidden * 2]
                newstate[prefix + 'bias_ni'] = value[2 * hidden:hidden * 3]
            elif name[:prefix_len + 7] == prefix + 'bias_hh':
                bias_r = bias_r + value[:hidden]
                bias_i = bias_i + value[hidden:hidden * 2]
                newstate[prefix + 'bias_nh'] = value[2 * hidden:hidden * 3]
            elif name[:prefix_len + 9] == prefix + 'weight_ih':
                newstate[prefix + 'weight_ri'] = (value[:hidden, :]).t()
                newstate[prefix + 'weight_ci'] = (value[hidden:hidden * 2, :]).t()
                newstate[prefix + 'weight_ni'] = (value[2 * hidden:hidden * 3, :]).t()
            elif name[:prefix_len + 9] == prefix + 'weight_hh':
                newstate[prefix + 'weight_rh'] = (value[:hidden, :]).t()
                newstate[prefix + 'weight_ch'] = (value[hidden:hidden * 2, :]).t()
                newstate[prefix + 'weight_nh'] = (value[2 * hidden:hidden * 3, :]).t()
            else:
                newstate[name] = value

        newstate[prefix + 'bias_r'] = bias_r
        newstate[prefix + 'bias_i'] = bias_i

        return newstate


class BidirGRULayer(nn.Module):
    __constants__ = ['directions']

    def __init__(self, input_size, hidden_size, weight_config, activation_config, norm_scale_out_config,
                 norm_scale_hidden_config,
                 compute_output_scale=False, compute_output_bit_width=False,
                 return_quant_tensor=False):
        super(BidirGRULayer, self).__init__()
        self.directions = nn.ModuleList([
            QuantGRULayer(input_size=input_size, hidden_size=hidden_size, weight_config=weight_config,
                          activation_config=activation_config,
                          norm_scale_out_config=norm_scale_out_config,
                          norm_scale_hidden_config=norm_scale_hidden_config,
                          reverse_input=False, compute_output_scale=compute_output_scale,
                          compute_output_bit_width=compute_output_bit_width,
                          return_quant_tensor=return_quant_tensor),
            QuantGRULayer(input_size=input_size, hidden_size=hidden_size, weight_config=weight_config,
                          activation_config=activation_config,
                          norm_scale_out_config=norm_scale_out_config,
                          norm_scale_hidden_config=norm_scale_hidden_config,
                          reverse_input=True, compute_output_scale=compute_output_scale,
                          compute_output_bit_width=compute_output_bit_width,
                          return_quant_tensor=return_quant_tensor),
        ])

    def forward(self, input, states):
        outputs = torch.jit.annotate(List[Tensor], [])
        output_states = torch.jit.annotate(List[Tensor], [])
        # XXX: enumerate https://github.com/pytorch/pytorch/issues/14471
        i = 0
        for direction in self.directions:
            state = states[i]
            out, out_state = direction(input, state)
            outputs += [out]
            output_states += [out_state]
            i += 1

        return torch.cat(outputs, -1), output_states

    def load_state_dict(self, state_dict, strict=True):
        direct = OrderedDict()
        reverse = OrderedDict()
        for name, value in state_dict.items():
            if name[-7:] == 'reverse':
                reverse[name] = value
            else:
                direct[name] = value

        self.directions[0].load_state_dict(direct, strict)
        self.directions[1].load_state_dict(reverse, strict)