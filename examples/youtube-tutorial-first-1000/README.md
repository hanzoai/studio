# How to Make Your First $1,000 With Hanzo.ai App Builder

A faceless YouTube tutorial, 6:58 at 1920×1080 and 30 fps, built from real Hanzo screens. The Studio graph that produces this kind of video is `user/default/workflows/youtube-tutorial-hanzo-engine.json`.

| File | Holds |
|---|---|
| `SCRIPT.md` | the voiceover, 13 lines |
| `STORYBOARD.md` | the 13 scenes and their timings |
| `frame.md` | palette and type |
| `NOTES.md` | captions, title, description, pinned comment, thumbnail text |
| `compositions/frames/` | one HyperFrames composition per scene |
| `assets/voice/`, `assets/bgm/` | Kokoro voice (am_adam, speed 0.86) and the MusicGen bed |
| `audio_meta.json` | word timings from Parakeet |

```sh
npm install
npm run check
npm run render    # writes renders/*.mp4
```
