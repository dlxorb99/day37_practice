INSERT INTO voice_runs
    (request_id, created_at, status, stt_ms, reply_ms,
     tts_ms, total_ms, model_version, audio_sha256)
VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)