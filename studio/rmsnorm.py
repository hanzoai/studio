import torch
import studio.model_management

RMSNorm = torch.nn.RMSNorm

def rms_norm(x, weight=None, eps=1e-6):
    if weight is None:
        return torch.nn.functional.rms_norm(x, (x.shape[-1],), eps=eps)
    else:
        return torch.nn.functional.rms_norm(x, weight.shape, weight=studio.model_management.cast_to(weight, dtype=x.dtype, device=x.device), eps=eps)
