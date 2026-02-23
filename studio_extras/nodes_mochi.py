from typing_extensions import override
import torch
import studio.model_management
import nodes
from studio_api.latest import StudioExtension, io


class EmptyMochiLatentVideo(io.StudioNode):
    @classmethod
    def define_schema(cls):
        return io.Schema(
            node_id="EmptyMochiLatentVideo",
            category="latent/video",
            inputs=[
                io.Int.Input("width", default=848, min=16, max=nodes.MAX_RESOLUTION, step=16),
                io.Int.Input("height", default=480, min=16, max=nodes.MAX_RESOLUTION, step=16),
                io.Int.Input("length", default=25, min=7, max=nodes.MAX_RESOLUTION, step=6),
                io.Int.Input("batch_size", default=1, min=1, max=4096),
            ],
            outputs=[
                io.Latent.Output(),
            ],
        )

    @classmethod
    def execute(cls, width, height, length, batch_size=1) -> io.NodeOutput:
        latent = torch.zeros([batch_size, 12, ((length - 1) // 6) + 1, height // 8, width // 8], device=studio.model_management.intermediate_device())
        return io.NodeOutput({"samples": latent})


class MochiExtension(StudioExtension):
    @override
    async def get_node_list(self) -> list[type[io.StudioNode]]:
        return [
            EmptyMochiLatentVideo,
        ]


async def studio_entrypoint() -> MochiExtension:
    return MochiExtension()
