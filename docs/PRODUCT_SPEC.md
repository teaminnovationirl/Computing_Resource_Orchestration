# Product spec: scope beyond the baseline

Decided on 2026-11-20 (D-82). Binding for every later slice. Where this file differs from
docs/IMPLEMENTATION_PLAN.md or docs/PROJECT_CONTEXT.md, this file wins, and each difference is logged
in docs/decisions.md (D-82 to D-87). Numbers marked "proposal" change only through a decision line.

## 1. Clients

One backend (the FastAPI server with the four engines) and one API serve three clients. No client
contains engine logic, and client machines never execute jobs.

- **Browser version** (used for the demo): works on desktop and on phones with no app to install.
  Every page is responsive and usable at 360 px width.
- **Windows client program** (.exe): the same pages in a native window (pywebview, packaged with
  PyInstaller). It finds the server on the LAN by itself (LAN guide D-21, accepted by D-83) and takes
  a server address typed by hand as the fallback.
- **Windows server program** (.exe): starts, stops and monitors the server engine and shows the admin
  console (capacity, queue, workers, running jobs, policy, cancel). The engine needs Linux with
  cgroups v2: on a Windows lab PC it runs in WSL2 with Docker, on a Linux PC natively. Which of the two
  the lab PC uses is decided before E1-3.

## 2. Code evaluation in four languages

| Language | Upload | Stored as | Compile | Run | Time limit multiplier |
| --- | --- | --- | --- | --- | --- |
| C | `.c` | `prog.c` | `gcc -O2 -std=c17 -o /out/prog /job/prog.c -lm` | `/out/prog` | x1 |
| C++ | `.cpp` | `prog.cpp` | `g++ -O2 -std=c++17 -o /out/prog /job/prog.cpp` | `/out/prog` | x1 |
| Java | `.java` | `Main.java` | `javac -d /out /job/Main.java` | `java -Xmx<M>m -XX:+UseSerialGC -cp /out Main` | x2 |
| Python | `.py` | `prog.py` | none | `python3 -I -B /job/prog.py` | x3 |

- Only the extension of the uploaded file's name counts, matched exactly in lower case (`.C` is
  refused: gcc would compile it as C++). The name itself is never stored, logged or used in a path
  (D-76). E2 checks this at submit (E2-1, built).
- E2 checks a `.java` file only for its extension and size. The judge (E1-1) compiles `Main.java`
  with javac; if no class Main can be run, the verdict is a compile error with the message
  "Java submissions must declare class Main".
- `M`, the JVM heap, is three quarters of the job's `ram_mib`; the rest is left for metaspace, thread
  stacks and the JIT (proposal).
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
- Output files are kept in the job's output folder under a size cap and can be downloaded only by the
  job's owner.

## 4. Two sandbox images

- **Judge image** (light): gcc, g++, OpenJDK and Python. All code evaluation uses it, so judging stays
  fast.
- **ML image**: the libraries of section 3, used only by `python_ml` jobs.

## 5. Notebook page (Google Colab style)

- Code cells and text cells. Running cell k submits one job that executes cells 1 to k in order, so
  variables carry over; the output is split back per cell.
- A status chip per cell: queued, running, then the verdict or the exit code.
- A side panel with the free CPU, RAM and GPU.
- File upload (zip, datasets) and output download.
- Works on phones.
- A persistent kernel per user is a later proposal, not built.

## 6. Which slice builds each part

| Part | Slice |
| --- | --- |
| Extension table, python_ml checks and seed row, upload caps in Config | E2-1 (built) |
| Judge commands per language, safe zip extraction, the Java class message | E1-1 |
| python_ml run path, log streaming, output capture | E1-2 |
| Both images, GPU pinning | E1-3 |
| Reference solutions in all four languages | E1-4 |
| Artifact download endpoint (owner only) | E4-3 |
| Log stream messages | E4-4 |
| Responsive pages, language picker, zip upload | C-2, C-3 |
| Notebook page | N-1 (new) |
| GPU on the lab PC, now required | X-7 |
| LAN discovery and agent, per the LAN guide | L-1 to L-3 (new) |
| Windows client program | D-1 (new) |
| Windows server program | D-2 (new) |

The slices D-1 and D-2 are build slices, not the decision lines D-01 and D-02 of docs/decisions.md.

## 7. Build order

E2-1, E2-2, E2-3, E2-4; E1-1, E1-2 (fake sandbox); E4-2, E4-3, E4-4; E4-5, E4-6, E4-7; C-1
(`SANDBOX_BACKEND=fake`); C-2, C-3; N-1; E1-3, E1-4 on the lab PC; C-4; X-1 to X-7; L-1 to L-3;
D-1, D-2.

No slice is skipped. The table order of plan 14.1 is not required, but each slice still starts only
when everything in its Needs column is merged (D-84).
