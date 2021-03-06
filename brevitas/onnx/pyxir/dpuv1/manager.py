from brevitas.onnx.base import BaseManager
from ...transform import move_domain_attributes_into_domain
from .handler import DPUv1QuantConv2dHandler, DPUv1QuantReLUHandler


class DPUv1Manager(BaseManager):

    handlers = [
        DPUv1QuantConv2dHandler,
        DPUv1QuantReLUHandler]

    model_transforms = [
        move_domain_attributes_into_domain]

    onnx_passes = [
        # use initializers instead of Constant nodes for fixed params
        "extract_constant_to_initializer",
        # remove unused graph inputs & initializers
        "eliminate_unused_initializer"]