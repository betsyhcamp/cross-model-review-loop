# Third-party notices

## claudex-loop

Parts of xreview are adapted from claudex-loop by Chase AI:

- `xreview/providers/codex.py`: the Codex command builder, the process runner with its timeout and process-group kill, and the event-stream checks.
- `tests/fake_codex.py` and `tests/test_codex_provider.py`: the fake Codex CLI and the resume, wrong-session, timeout and missing-completion tests.

Source: https://github.com/chaseai-yt/claudex-loop, at commit `8cf5e2c1771c5151d90c12642391d0ba8fa71b0e`.

```text
MIT License

Copyright (c) 2026 Chase AI

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```
