# benchcode --deep, 2026-11-09, M1 Max 64 GB

Thinking off on every row. One run per pair.

| Row | Kapitsa 27B | Kapitsa 35B-A3B | Kapitsa Flash-Next | oMLX 27B | oMLX 35B-A3B | mlx-serve 27B | mlx-serve 35B-A3B | Ollama 27B MLX | Ollama 35B MLX |
|---|---|---|---|---|---|---|---|---|---|
| Writing T=0.6 tok/s | 63.2 | 145 | 26.7 | 36.7 | 83.0 | 43.7 | 99.0 | 23.6 | 78.3 |
| Writing greedy tok/s | 68.6 | 159 | 27.4 | 32.0 | 83.8 | 48.3 | 104 | 25.2 | 84.2 |
| Reading 4K tok/s | 133 | 1155 | 288 | 159 | 786 | 133 | 884 | 105 | 719 |
| Reading 16K tok/s | 127 | 940 | 258 | 152 | 723 | 120 | 674 | 101 | 668 |
| Reading 32K tok/s | 109 | 779 | 247 | 142 | 624 | 105 | 507 | 96.3 | 582 |
| Reading 64K tok/s | 93.6 | 576 | 204 | 124 | 482 | 84.4 | 339 | 85.0 | 445 |
| Reading 128K tok/s | 82.4 | 376 | 148 | 94.8 | 327 | 60.3 | 203 | 67.1 | 276 |
| Follow-up 4K s | 0.28 | 0.08 | 0.43 | 0.92 | 0.34 | 0.47 | 0.36 | 0.60 | 0.22 |
| Follow-up 32K s | 0.41 | 0.13 | 0.44 | 1.46 | 0.54 | 0.83 | 0.83 | 0.74 | 0.38 |
| Follow-up 128K s | 0.80 | 0.30 | 0.52 | 3.63 | 1.39 | 2.12 | 72.9 | 1.52 | 0.94 |
| Writing at 4K tok/s | 55.5 | 183 | 25.5 | 33.0 | 76.8 | 34.6 | 92.8 | 22.3 | 70.3 |
| Writing at 32K tok/s | 44.8 | – | 24.9 | 28.0 | 55.3 | 16.4 | 64.9 | 17.5 | 48.4 |
| Writing at 128K tok/s | 22.6 | 60.4 | 22.5 | 15.4 | 28.5 | 7.9 | 29.3 | 9.1 | 29.5 |
| Agent step 4K s | 35.7 | 5.11 | 20.8 | 31.1 | 6.90 | 38.2 | 7.18 | 46.2 | 7.46 |
| Agent step 32K s | 50.8 | 8.93 | 21.8 | 44.8 | 13.1 | 60.7 | 16.0 | 55.6 | 11.3 |
| Agent step 128K s | 74.9 | 21.4 | 45.6 | 78.8 | 25.4 | – | 115 | 110 | 29.6 |
| Recall, all depths | 3/3/3/3/3 | 3/3/3/3/3 | 3/3/3/3/3 | 3/3/3/3/3 | 3/3/3/3/3 | 3/3/3/3/3 | 3/3/3/3/3 | 3/3/3/3/3 | 3/3/3/3/3 |
| Memory at 128K GB | 41.3 | 29.3 | 47.2 | 31.1 | 41.5 | 28.9 | 24.1 | 30.5 | 34.6 |
| Cache: new question / after other | hit / kept | hit / kept | hit / lost | hit / kept | hit / kept | miss / kept | miss / kept | miss / kept | miss / kept |
| Tool calls valid | 10/10 | 10/10 | 10/10 | 10/10 | 10/10 | 10/10 | 10/10 | 10/10 | 10/10 |
| Forced choice | honoured | honoured | honoured | ignored | ignored | honoured | honoured | ignored | ignored |
| Session steps, context | 8, 13K→79K | 8, 13K→79K | 8, 13K→79K | 8, 13K→79K | 8, 13K→79K | 8, 13K→79K | 8, 13K→79K | 8, 13K→79K | 8, 13K→79K |
| Session step median s | 86.3 | 18.7 | 45.4 | 80.9 | 24.6 | 121 | 33.2 | 107 | 23.2 |
| Session total min | 13.5 | 2.7 | 6.9 | 12.3 | 3.5 | 19.7 | 5.2 | 16.7 | 3.6 |
| Session well-formed replies | 9/9 | 8/9 | 8/9 | 9/9 | 9/9 | 9/9 | 9/9 | 9/9 | 9/9 |
| Compaction s | 75.0 | 21.1 | 97.9 | 96.6 | 197 | 272 | 295 | 212 | 49.2 |
| Compaction: conversation cached | 100% | 100% | 100% | 100% | 15% | 92% | 16% | 92% | 92% |
| Next turn after compaction s | 20.8 | 3.33 | 57.9 | 22.8 | 3.86 | 64.4 | 11.8 | 70.2 | 10.6 |
| Sustain 5 min | -9% | +2% | +1% | +0% | -1% | -1% | -1% | +1% | -0% |
| Sustain vs fresh | -11% | +2% | -0% | -6% | -2% | -46% | -0% | -7% | -24% |
| Memory peak GB | 44.4 | 30.4 | 49.8 | 46.5 | 47.3 | 51.2 | 31.9 | 60.2 | 58.7 |
| Thinking default | on, 52% reasoning | on, 91% reasoning | on, 46% reasoning | on, 49% reasoning | on, 100% reasoning | off, 47% reasoning | off, 100% reasoning | on, 45% reasoning | on, 100% reasoning |
| Busy at start | none | none | launchd 48% | none | none | none | none | none | none |
| INVALID | no | no | no | no | no | no | no | reading 128K: content contains </think> | no |
| Elapsed min | 80.0 | 24.9 | 53.1 | 75.5 | 29.5 | 117 | 45.4 | 108 | 28.9 |

Notes
- Thinking off on every row (both switches sent). mlx-serve logs thinking=false even when no switch is sent: its server default is off.
- mlx-serve 27B, agent step at 128K: the server refused the 135,627-token prompt ("does not fit in GPU memory even after freeing").
- mlx-serve 35B-A3B, follow-up at 128K: 122,880 of 131,120 tokens reused, so the turn re-read the rest (72.9 s).
- oMLX 35B-A3B and mlx-serve 35B-A3B: only 15-16% of the 80K conversation was cached when the summary was requested.
- Kapitsa 35B-A3B, writing at 32K: all five attempts stopped before 512 tokens; the row is empty.
- Ollama 27B MLX: INVALID at 128K, the answer contained </think> with thinking off; 60 GB peak and swap at 128K.
- Kapitsa Flash-Next: the prefix is lost after another conversation; the turn after compaction re-read the 15K system prompt (57.9 s).
