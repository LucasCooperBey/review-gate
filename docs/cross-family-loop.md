# Cross-family loop

Models from one family tend to share training and blind spots, so a second family catches some different mistakes. It does not prove the code correct.

## Steps

1. Write the change with one family. Record that family as the author:

   ```sh
   export REVIEW_GATE_AUTHOR=claude
   ```

   Use `codex` when Codex wrote the change, or `other` when the author is neither.

2. Push, or run review-gate with a reviewer from another family. `auto` reads the author and picks the first installed reviewer from a different family:

   ```sh
   git push
   python3 review_gate.py --round 1 --reviewer auto --author claude
   ```

   To name the reviewer yourself:

   ```sh
   python3 review_gate.py --round 1 --reviewer codex --author claude
   ```

3. Fix each finding, or write down why it is wrong.

4. Rerun the same review and raise the round:

   ```sh
   python3 review_gate.py --round 2 --reviewer auto --author claude
   ```

5. Stop when the verdict is PASS, or after three rounds. `--round` is stored on each spine record (default 1), so the log shows how many rounds it took:

   ```sh
   python3 review_gate.py --round 3 --reviewer auto --author claude
   python3 review_gate.py --stats
   ```
