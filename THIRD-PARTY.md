# Third-party material

## LongMemEval (benchmark data and grading prompts)

- What we use:
  - **The grading prompts.** The answer-check prompts in `bench/lme.py`
    (`anscheck_prompt`) are copied unchanged from LongMemEval's
    `src/evaluation/evaluate_qa.py`. Every correctness grade in this repo and in
    `data/` comes from them.
  - **The benchmark data.** LongMemEval-S (cleaned) is downloaded at run time into
    `out/lme/` and is not copied into this repo. `data/` contains material derived
    from it: question ids, question types, LongMemEval's gold answers (the `gold`
    field of every results row), and Deltos's memory-building output for the
    held-out questions (`data/cache/extract-heldout.jsonl`), which restates facts
    from LongMemEval's chats. Some of those chats are drawn from public chat
    collections (session ids starting `sharegpt_` and `ultrachat_`), so short
    excerpts of them appear there as LongMemEval distributes them.
- Source: https://github.com/xiaowu0162/LongMemEval · data:
  https://huggingface.co/datasets/xiaowu0162/longmemeval-cleaned
- Paper: Di Wu, Hongwei Wang, Wenhao Yu, Yuwei Zhang, Kai-Wei Chang, Dong Yu,
  "LongMemEval: Benchmarking Chat Assistants on Long-Term Interactive Memory",
  ICLR 2025 (arXiv:2410.10813).
- License: MIT. The GitHub repository's `LICENSE` reads "Copyright (c) 2024 Di Wu";
  the Hugging Face dataset card declares `license: mit`. Its text:

```
MIT License

Copyright (c) 2024 Di Wu

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

## SCOWL word list (bundled)

- What: `deltos/words.txt.gz` holds the lowercase words of SCOWL's American
  English list. Recall uses it to tell a plain word from a name: a one-word thing
  named "Port" or "Run" counts only where a question writes it as a name. Until
  10-17 it was read from the system's `/usr/share/dict/words`, so a machine
  without that file behaved differently.
- Source: SCOWL (Spell Checker Oriented Word Lists) by Kevin Atkinson,
  http://wordlist.sourceforge.net/, as Debian/Ubuntu ship it (package `wamerican`
  2020.12.07-2).
- License: permissive, with notices to keep. The collective work is "Copyright
  2000-2011 by Kevin Atkinson" with permission "to use, copy, modify, distribute
  and sell these word lists ... provided that the above copyright notice appears
  in all copies and that both that copyright notice and this permission notice
  appear in supporting documentation"; parts come from sources with their own
  notices (Moby Words II, public domain; WordNet 1.6, Princeton University; the UK
  English word list, J Ross Beresford; Ispell, Geoff Kuenning). All of them,
  unchanged: `deltos/words.LICENSE`.

## LoCoMo (second benchmark, not bundled)

- What we use: the ten long conversations and their 1,986 questions, as a fresh
  benchmark after LongMemEval. `bench/locomo.py` downloads `locomo10.json` at run
  time into `out/locomo/` (git-ignored) and converts it there; no LoCoMo text is
  copied into this repo. Grading uses LongMemEval's prompts (above), so LoCoMo's own
  F1 numbers are not what we report.
- Source: https://github.com/snap-research/locomo
- Paper: Adyasha Maharana, Dong-Ho Lee, Sergey Tulyakov, Mohit Bansal, Francesco
  Barbieri, Yuwei Fang, "Evaluating Very Long-Term Conversational Memory of LLM
  Agents", ACL 2024 (arXiv:2402.17753).
- License: CC BY-NC 4.0 (non-commercial use, with attribution). Results files that
  quote LoCoMo questions or answers carry the same license and attribution, and stay
  out of anything sold.

## Mem0 and Graphiti (compared systems, not bundled)

- What: `bench/others.py` runs them as installed packages in a separate,
  git-ignored virtualenv; no code of theirs is copied into this repo. For Mem0,
  the bench wraps its extraction-prompt builder so the prompt's observation date
  is each chat's own date (`timestamp` / `current_date`) instead of today's (see
  the file's header). `data/results/vs-others/` holds their answers to our
  questions and `data/costs/` their memory-building costs, as measured here.
- Mem0: https://github.com/mem0ai/mem0 (package `mem0ai` 2.2.1) · License: Apache-2.0.
- Graphiti: https://github.com/getzep/graphiti (package `graphiti-core` 0.30.2) ·
  License: Apache-2.0.
- The Graphiti context layout (facts with their valid date ranges, then entity
  summaries) follows the template in Preston Rasmussen et al., "Zep: A Temporal
  Knowledge Graph Architecture for Agent Memory", arXiv:2501.13956 (2025), which
  Zep used for its LongMemEval runs.

## Synthetic-world benchmark references

- The `grep` adapter (`bench/adapters/grep_agent.py`) follows the plain-files
  memory style described by Agent Memory Repo
  (https://github.com/AgentMemoryRepo/agentmemoryrepo); no code is copied.
- The `gbrain` adapter (`bench/adapters/gbrain.py`) runs an installed
  [gbrain](https://github.com/garrytan/gbrain) as an external command; no code is
  copied.
