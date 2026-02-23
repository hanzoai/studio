import node_helpers
from typing_extensions import override
from studio_api.latest import StudioExtension, io


class ReferenceLatent(io.StudioNode):
    @classmethod
    def define_schema(cls):
        return io.Schema(
            node_id="ReferenceLatent",
            category="advanced/conditioning/edit_models",
            description="This node sets the guiding latent for an edit model. If the model supports it you can chain multiple to set multiple reference images.",
            inputs=[
                io.Conditioning.Input("conditioning"),
                io.Latent.Input("latent", optional=True),
            ],
            outputs=[
                io.Conditioning.Output(),
            ]
        )

    @classmethod
    def execute(cls, conditioning, latent=None) -> io.NodeOutput:
        if latent is not None:
            conditioning = node_helpers.conditioning_set_values(conditioning, {"reference_latents": [latent["samples"]]}, append=True)
        return io.NodeOutput(conditioning)


class EditModelExtension(StudioExtension):
    @override
    async def get_node_list(self) -> list[type[io.StudioNode]]:
        return [
            ReferenceLatent,
        ]


def studio_entrypoint() -> EditModelExtension:
    return EditModelExtension()
