from typing_extensions import override
import torch
import torch.nn.functional as F

from studio_api.latest import StudioExtension, io


class Mahiro(io.StudioNode):
    @classmethod
    def define_schema(cls):
        return io.Schema(
            node_id="Mahiro",
            display_name="Mahiro CFG",
            category="_for_testing",
            description="Modify the guidance to scale more on the 'direction' of the positive prompt rather than the difference between the negative prompt.",
            inputs=[
                io.Model.Input("model"),
            ],
            outputs=[
                io.Model.Output(display_name="patched_model"),
            ],
            is_experimental=True,
        )

    @classmethod
    def execute(cls, model) -> io.NodeOutput:
        m = model.clone()
        def mahiro_normd(args):
            scale: float = args['cond_scale']
            cond_p: torch.Tensor = args['cond_denoised']
            uncond_p: torch.Tensor = args['uncond_denoised']
            #naive leap
            leap = cond_p * scale
            #sim with uncond leap
            u_leap = uncond_p * scale
            cfg = args["denoised"]
            merge = (leap + cfg) / 2
            normu = torch.sqrt(u_leap.abs()) * u_leap.sign()
            normm = torch.sqrt(merge.abs()) * merge.sign()
            sim = F.cosine_similarity(normu, normm).mean()
            simsc = 2 * (sim+1)
            wm = (simsc*cfg + (4-simsc)*leap) / 4
            return wm
        m.set_model_sampler_post_cfg_function(mahiro_normd)
        return io.NodeOutput(m)


class MahiroExtension(StudioExtension):
    @override
    async def get_node_list(self) -> list[type[io.StudioNode]]:
        return [
            Mahiro,
        ]


async def studio_entrypoint() -> MahiroExtension:
    return MahiroExtension()
