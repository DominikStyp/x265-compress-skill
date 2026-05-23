"""Sub-modules for run_queue.py — split out so run_queue.py stays a slim
queue-loop orchestrator instead of a 450-line everything-bag.

Layout:
  job_schema  -- VALID_KEYS, merge_job, build_compress_argv, derive_output_path,
                 expand_jobs. The shape of a job + how it maps to compress.py.
  queue_io    -- load_queue + reload_queue_with_retry. JSON I/O with the
                 mid-edit retry that lets users tweak queue.json mid-flight.
  job_runner  -- run_one_job: invoke compress.py, run the generated .bat,
                 read the quality sidecar, build the per-job report row.
"""
