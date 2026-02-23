import nodes

from typing_extensions import override
from studio_api.latest import IO, StudioExtension


class ImageCompare(IO.StudioNode):
    """Compares two images with a slider interface."""

    @classmethod
    def define_schema(cls):
        return IO.Schema(
            node_id="ImageCompare",
            display_name="Image Compare",
            description="Compares two images side by side with a slider.",
            category="image",
            is_experimental=True,
            is_output_node=True,
            inputs=[
                IO.Image.Input("image_a", optional=True),
                IO.Image.Input("image_b", optional=True),
                IO.ImageCompare.Input("compare_view"),
            ],
            outputs=[],
        )

    @classmethod
    def execute(cls, image_a=None, image_b=None, compare_view=None) -> IO.NodeOutput:
        result = {"a_images": [], "b_images": []}

        preview_node = nodes.PreviewImage()

        if image_a is not None and len(image_a) > 0:
            saved = preview_node.save_images(image_a, "studio.compare.a")
            result["a_images"] = saved["ui"]["images"]

        if image_b is not None and len(image_b) > 0:
            saved = preview_node.save_images(image_b, "studio.compare.b")
            result["b_images"] = saved["ui"]["images"]

        return IO.NodeOutput(ui=result)


class ImageCompareExtension(StudioExtension):
    @override
    async def get_node_list(self) -> list[type[IO.StudioNode]]:
        return [
            ImageCompare,
        ]


async def studio_entrypoint() -> ImageCompareExtension:
    return ImageCompareExtension()
