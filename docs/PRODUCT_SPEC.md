# Product spec: scope beyond the baseline

Decided on 2026-11-20 (D-82) and extended on 2026-11-21 (D-88 to D-102). Binding for every later slice.
Where this file differs from docs/IMPLEMENTATION_PLAN.md or docs/PROJECT_CONTEXT.md, this file wins, and
each difference is logged in docs/decisions.md (D-82 to D-102). Numbers marked "proposal" change only
through a decision line.

## 1. Clients and deployment

One backend (the FastAPI server with the four engines) and one API serve three clients. No client
contains engine logic, and client machines never execute jobs.

- **Browser version** (used for the demo): works on desktop and on phones with no app to install.
  Every page is responsive and usable at 360 px width.
- **Windows client program** (.exe, slice W-1): the same pages in a native window (pywebview, packaged
  with PyInstaller). It finds the server on the LAN by itself (LAN guide D-21, accepted by D-83) and
  takes a server address typed by hand as the fallback.
- **Windows server program** (.exe, slice W-2): starts, stops and monitors the server engine and shows
  the admin console (capacity, queue, workers, running jobs, policy, cancel). The engine needs Linux
  with cgroups v2: on a Windows lab PC it runs in WSL2 with Docker, on a Linux PC natively. Which of
  the two the lab PC uses is decided before E1-3.
- **HTTPS on the LAN** with the team certificate (LAN guide D-27, accepted by D-99): each phone and
  laptop trusts the team's `rootCA.pem` once, a setup step in the README (C-4). `rootCA-key.pem` is
  never shared.
- **Fallback** if the lab PC is not available: the team laptop runs the engine in WSL2 or a Linux VM,
  CPU only (D-101).

## 2. Code evaluation in four languages

| Language | Upload | Stored as | Compile | Run | Time limit multiplier |
| --- | --- | --- | --- | --- | --- |
| C | `.c` | `prog.c` | `gcc -O2 -std=c17 -o /out/prog /job/prog.c -lm` | `/out/prog` | x1 |
| C++ | `.cpp` | `prog.cpp` | `g++ -O2 -std=c++17 -o /out/prog /job/prog.cpp` | `/out/prog` | x1 |
| Java | `.java` | `Main.java` | `javac -encoding UTF-8 -d /out /job/Main.java` | `java -Xmx<M>m -Xss64m -XX:+UseSerialGC -cp /out Main` | x2 |
| Python | `.py` | `prog.py` | a syntax check that runs no code (D-145) | `python3 -I -B /job/prog.py` | x3 |

- Submissions are full programs that read stdin and write stdout, in all four languages (D-91).
  Function-signature templates (`class Solution`) are a later proposal, because they need a wrapper per
  problem and per language.
- Only the extension of the uploaded file's name counts, matched exactly in lower case (`.C` is
  refused: gcc would compile it as C++). The name itself is never stored, logged or used in a path
  (D-76). E2 checks this at submit (E2-1, built).
- E2 checks a `.java` file only for its extension and size. The judge (E1-1) compiles `Main.java`
  with javac; if no class Main can be run, the verdict is a compile error with the message
  "Java submissions must declare class Main".
- `M`, the JVM heap, is three quarters of the job's `ram_mib`; the rest is left for metaspace, thread
  stacks and the JIT (proposal).
- Stack, so deep recursion does not crash (D-100, proposal): C and C++ get a stack limit equal to the
  job's memory limit (`docker --ulimit stack`), and Java gets `-Xss64m`. E1-1 builds the commands, E1-3
  adds the ulimit.
- The multiplier scales the problem's per-case limit (`problems.time_limit_ms`) for that language
  (proposal). Every compile step keeps `COMPILE_TIMEOUT_S`.
- The C and C++ commands are plan 9.4's. The Java and Python commands and the JVM flags are proposals.

## 3. python_ml jobs (machine learning and deep learning)

- Workload type `python_ml` (db/seed.sql): default need class 3, like the other general types.
  `max_time_limit_s` is 3,600 now; 14,400 is a proposal that needs the CHECK on
  `workload_types.max_time_limit_s` widened, which is a schema change not yet made (D-85).
- Payload: one `.py` file, stored as `prog.py`, or one `.zip`, stored as `payload.zip`, with `main.py`
  at its top level. Before anything is stored, E2 checks that the zip opens; has no absolute path, no
  `..` part and no link, under both the name stored and the name zipfile extracts to; has at most `PYTHON_ML_MAX_ENTRIES` entries (10,000) whose declared sizes
  total at most `PYTHON_ML_UNZIPPED_MAX_BYTES` (4 GiB); and that the upload is at most
  `PYTHON_ML_UPLOAD_MAX_BYTES` (1 GiB). The three are Config settings (proposals, D-85).
- E1-1 extracts the zip safely: the same caps hold for the bytes actually written, because a zip's
  declared sizes can lie, and nothing lands outside the job's folder.
- No network inside the sandbox (baseline). Datasets and weights come in the zip or from a read-only
  shared datasets folder that administrators fill.
- Libraries are pre-installed in a separate ML image: Python, numpy, pandas, scikit-learn,
  matplotlib, PyTorch and TensorFlow with CUDA.
- The allocated GPU is pinned with `docker run --gpus device=<gpu_index>`. VRAM is enforced by
  admission (E3), not by the kernel.
- Logs stream to the job page while the job runs, throttled.
- Output files are kept in the job's output folder under a size cap and are downloaded as section 4
  says.

## 4. Uploads and downloads

- Uploads are spooled to a file on disk and never held whole in memory (D-90). E4-3 streams each
  upload into `DATA_DIR/spool`, reading at most the type's cap plus one byte, and deletes a spool file
  that is still there after submit() returns or raises.
- `E2Port.submit(user, req, source: Path)`: the source is the spooled file instead of bytes, an
  approved interface change made in E2-5. submit() refuses any path that does not resolve inside
  `DATA_DIR/spool` (ValueError, nothing written), validates the file in chunks (zipfile opens it by
  path) and moves it into `payloads/<hex>/<stored name>` with `os.replace`.
- `DATA_DIR/spool` sits on the same drive as `DATA_DIR/payloads`, so the move is a true rename:
  `os.replace` never copies, and a spool folder on another drive fails loudly.
- E2-5 adapts the E2-1 tests to pass a spooled file instead of bytes, with the same checks and the same
  expected results, so every behaviour they prove stays proven.
- Output downloads follow the baseline role rules (FR-15): the job's owner, faculty and administrators
  (D-98).

## 5. Two sandbox images

- **Judge image** (light): gcc, g++, OpenJDK and Python. All code evaluation and every practice run use
  it, so judging stays fast.
- **ML image**: the libraries of section 3, used only by `python_ml` jobs.

## 6. Code workspace (LeetCode style)

For C, C++, Java and Python (D-91 to D-95).

- **Problem list page**: every problem, with the user's best verdict on it.
- **Problem page**, a split view. On the left the statement: Markdown from `statement.md`, rendered
  through the vendored marked and DOMPurify of section 8, with its sample cases. On the right the code
  editor with a language dropdown and a starter template per language (for Java, `public class Main`
  with `main`); below it a Testcase tab (editable input, sample 1 by default) and a Result tab (output,
  the expected output when the input is a sample, exit code, stderr, time and memory).
- **Run**: a practice run (section 7) of the editor's code on the Testcase input, with no verdict.
  **Submit**: judged on every hidden test case, with the verdict and time per case and the overall
  verdict. A **Submissions** tab lists the user's earlier submissions for that problem.
- **Playground page**: editor, language, input and Run, with no problem, for classes and studying.
- **Samples** are stored as `samples/<n>.in` and `samples/<n>.out` in the problem's folder beside
  `statement.md` (no schema change). They are shown with the statement and never used for judging.
  E1-4 adds a statement and two samples (proposal) for seed problem 12.
- **Editor**: vendored CodeMirror 5 (no build step, works offline on the LAN), with syntax highlighting
  and line numbers for all four languages. Unsaved code is kept as a draft in the browser, per problem
  and language. C-2 vendors CodeMirror 5, marked and DOMPurify, each pinned to one release.
- **Memory** per test case is not stored, because the schema has `time_ms` only; memory is shown for
  practice runs. Per-case memory is a later proposal.

## 7. Practice runs (workload type code_run)

- A new workload type `code_run` (seed row; default need class 3, as the other general types) runs a
  program in any of the four languages once, with the user's own input, no problem and no verdict, and
  returns the output, stderr, exit code, time and memory (D-96).
- `SubmitRequest` gains an optional `stdin`, at most 1 MiB (proposal), stored as `input.txt` beside the
  source. Only code_run takes a stdin; the other types refuse one (proposal).
- The code_run time limit is 10 s: `max_time_limit_s` 10 in its seed row (proposal).
- `attempts` has no columns for output, exit code or memory, so a practice run's results are kept in
  its run folder under DATA_DIR and served by E4-3.
- Slice E2-5, right after E2-4, builds the validation and the seed row; E1-1 runs the program once with
  that input.

## 8. Notebook (Colab style)

For Python (D-97). It runs Python as python_ml jobs; C, C++ and Java use the code workspace.

- A runtime panel sets CPU, RAM, GPU memory and the time limit within the allowed bounds, like Colab's
  "Change runtime type".
- Code cells and text cells. Running cell k runs cells 1 to k as one job, so variables carry over, and
  the output is split back per cell. "Run all" runs every cell.
- A status chip per cell (queued, running, then the verdict or the exit code), and a side panel with
  the free CPU, RAM and GPU.
- matplotlib uses the Agg backend, and every figure a cell shows is saved and displayed under that cell
  (E1-2 captures them).
- Text cells render Markdown through vendored marked and DOMPurify, so no script can run. All job
  output is shown as text, never as HTML.
- Notebooks are saved on the server as one file per notebook under DATA_DIR (no new table), owned by
  their user, and can be exported and imported as `.ipynb`.
- Dataset upload (zip) and output download. It works on phones.
- A persistent kernel per user is a later proposal, not built.

## 9. Which slice builds each part

| Slice | Builds |
| --- | --- |
| E2-1 (built) | Extension table, python_ml checks and seed row, upload caps in Config |
| E2-5 (new) | Practice-run validation and the code_run seed row; submit() from the spooled file (section 4) |
| E1-1 | Judge in four languages, practice runs, stack limits, safe zip extraction, the Java class message |
| E1-2 | python_ml run path, log streaming, figure and output capture |
| E1-3 | Both images, GPU pinning, the stack ulimit |
| E1-4 | Reference solutions in four languages, plus problem 12's statement and samples |
| E4-3 | Upload spooling; endpoints for problems, statements, samples, submission history and downloads |
| E4-4 | Log stream messages |
| C-2 | Login, problem list, problem page, playground, my jobs and job detail, all responsive |
| C-3 | Live monitor and admin console |
| N-1 (new) | The notebook page with its save, load, export and import endpoints |
| X-3 | Faculty upload of problems with statement and samples |
| X-7 | GPU on the lab PC, now required |
| L-1 to L-3 (new) | LAN discovery and agent, per the LAN guide |
| W-1 (new) | Windows client program |
| W-2 (new) | Windows server program |

## 10. Build order

E2-1, E2-2, E2-3, E2-4, E2-5; E1-1; E2-6 (D-153); E1-2 (fake sandbox); E4-2, E4-3, E4-4; E4-5, E4-6,
E4-7; C-1 (`SANDBOX_BACKEND=fake`); C-2, C-3; N-1; E1-3, E1-4 on the lab PC; C-4; X-1 to X-7; L-1 to
L-3; W-1, W-2 (D-102).

No slice is skipped. The table order of plan 14.1 is not required, but each slice still starts only
when everything in its Needs column is merged (D-84).
