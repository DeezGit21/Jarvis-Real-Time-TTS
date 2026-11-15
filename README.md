# 🐴 Jarvis — Real Time TTS

**This repo has been merged into [jarvis-deck](https://github.com/DeezGit21/jarvis-deck).**

The full project now lives at
**[jarvis-deck/tree/main/tts](https://github.com/DeezGit21/jarvis-deck/tree/main/tts)** —
same MIT license, same self-contained voice loop (push-to-talk → streaming
LLM → sentence-by-sentence streaming TTS), plus the agent handoff that
executes real actions locally via the Hermes one-shot agent.

```bash
git clone https://github.com/DeezGit21/jarvis-deck
cd jarvis-deck/tts
pip install -r requirements.txt
python server.py        # http://127.0.0.1:8899
```

Want the whole dashboard? That's [jarvis-deck](https://github.com/DeezGit21/jarvis-deck).
