"""Sub-modules for compress.py — split out so compress.py stays a slim
CLI orchestrator instead of a 750-line everything-bag.

Layout:
  x265_params  -- BASE_X265_PARAMS, HIGH_BIT_DEPTH_FMTS (pure data).
  probe        -- ffprobe wrapper + SourceInfo dataclass + analyse().
  plan         -- EncodePlan + pick_crf/pick_preset/pick_parallel + plan_encode().
  bat_writer   -- .bat templates + write_bat() + small format helpers.
"""
