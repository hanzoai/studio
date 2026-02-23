import folder_paths
import studio.audio_encoders.audio_encoders
import studio.utils
from typing_extensions import override
from studio_api.latest import StudioExtension, io


class AudioEncoderLoader(io.StudioNode):
    @classmethod
    def define_schema(cls) -> io.Schema:
        return io.Schema(
            node_id="AudioEncoderLoader",
            category="loaders",
            inputs=[
                io.Combo.Input(
                    "audio_encoder_name",
                    options=folder_paths.get_filename_list("audio_encoders"),
                ),
            ],
            outputs=[io.AudioEncoder.Output()],
        )

    @classmethod
    def execute(cls, audio_encoder_name) -> io.NodeOutput:
        audio_encoder_name = folder_paths.get_full_path_or_raise("audio_encoders", audio_encoder_name)
        sd = studio.utils.load_torch_file(audio_encoder_name, safe_load=True)
        audio_encoder = studio.audio_encoders.audio_encoders.load_audio_encoder_from_sd(sd)
        if audio_encoder is None:
            raise RuntimeError("ERROR: audio encoder file is invalid and does not contain a valid model.")
        return io.NodeOutput(audio_encoder)


class AudioEncoderEncode(io.StudioNode):
    @classmethod
    def define_schema(cls) -> io.Schema:
        return io.Schema(
            node_id="AudioEncoderEncode",
            category="conditioning",
            inputs=[
                io.AudioEncoder.Input("audio_encoder"),
                io.Audio.Input("audio"),
            ],
            outputs=[io.AudioEncoderOutput.Output()],
        )

    @classmethod
    def execute(cls, audio_encoder, audio) -> io.NodeOutput:
        output = audio_encoder.encode_audio(audio["waveform"], audio["sample_rate"])
        return io.NodeOutput(output)


class AudioEncoder(StudioExtension):
    @override
    async def get_node_list(self) -> list[type[io.StudioNode]]:
        return [
            AudioEncoderLoader,
            AudioEncoderEncode,
        ]


async def studio_entrypoint() -> AudioEncoder:
    return AudioEncoder()
