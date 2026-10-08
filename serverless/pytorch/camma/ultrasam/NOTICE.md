# Third-party notices

This integration uses [CAMMA-public/UltraSam](https://github.com/CAMMA-public/UltraSam)
at commit `ff3157b1fca8b1d963d9138372768e1fecad71e9`.
UltraSAM is distributed under Creative Commons Attribution-NonCommercial-ShareAlike
4.0 International. The license text is included in `LICENSE-UltraSAM`.
Commercial use requires an appropriate separate grant from the rights holder.

`nuclio/ultrasam_prompt_adapter.py` adapts the upstream prompt construction for
one object with multiple positive and negative points. It preserves the original
embedding indices and calls the original prompt encoding, decoder and mask head.
Its adaptations are licensed under CC BY-NC-SA 4.0, as indicated in the file.
The original upstream source and checkpoint are downloaded during the image build.
The integration does not change the licensing of either artifact.

Other integration files identify their license with individual SPDX headers.
Dependency distributions retain their own licenses in the installed environment.
