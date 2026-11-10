# Default thinking, 2026-11-09, M1 Max 64 GB

No thinking switch sent. Same session shape as the thinking-off rows (12K system prompt, 8 steps of 8K tool results).

| | Kapitsa 27B | Kapitsa 35B | Kapitsa Flash | oMLX 27B | oMLX 35B | mlx-serve 27B | mlx-serve 35B | Ollama 27B | Ollama 35B |
|---|---|---|---|---|---|---|---|---|---|
| Reasoning share at default | 59% | 91% | 57% | 54% | 89% | off by default | off by default | 63% | 92% |
| Tool call, default thinking, s | 1.5 | 0.9 | 3.0 | 2.7 | 2.0 | same as off | same as off | 3.8 | 1.9 |
| Reasoning tokens a call | 28.0 | 65.5 | 24.5 | 28.5 | 91.0 | – | – | 73.5 | 128 |
| Session step off, s | 86.3 | 18.7 | 45.4 | 80.9 | 24.6 | 121 | 33.2 | 107 | 23.2 |
| Session step default, s | 101 | 19.4 | 54.2 | 90.1 | 25.8 | same as off | same as off | 112 | 24.8 |
| Ratio | 1.17x | 1.04x | 1.20x | 1.11x | 1.05x | 1.0x | 1.0x | 1.05x | 1.07x |
| Reasoning tokens a step | 69.0 | 23.5 | 69.0 | 81.5 | 30.0 | – | – | 106 | 80.5 |
| Well-formed replies, default | 9/9 | 9/9 | 9/9 | 9/9 | 7/9 | – | – | 9/9 | 9/9 |
| Compaction off, s | 75.0 | 21.1 | 97.9 | 96.6 | 197 | 272 | 295 | 212 | 49.2 |
| Compaction default, s | no summary | 32.0 | 219 | 307 | 58.7 | same as off | same as off | 227 | 227 |
| Reasoning tokens in compaction | 6144 | 648 | 1241 | 3369 | 758 | – | – | 726 | 628 |
| Next turn off, s | 20.8 | 3.3 | 57.9 | 22.8 | 3.9 | 64.4 | 11.8 | 70.2 | 10.6 |
| Next turn default, s | – | 4.2 | 64.2 | 34.4 | 6.3 | same as off | same as off | 84.7 | 12.2 |
