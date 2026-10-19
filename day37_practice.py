# day37_practice.py
# 캠퍼스 시설 안내 음성 도우미

import io
from faster_whisper import WhisperModel
import edge_tts
import psycopg
import asyncio # 비동기 TTS 함수를 실행
import hashlib # 원본 음성의 SHA-256 해시값 계산
import json # 실행 기록을 JSONL 형식으로 저장
import os # DATABASE_URL 환경변수 조회
import tempfile # STT 입력을 위한 임시 WAV파일 생성
import time # 처리 단계별 실행 시간 측정
import uuid # 요청별 고유 ID 생성
import wave # WAV 파일 헤더와 PCM 형식 검사
from datetime import datetime, timezone # UTC 시각 생성
from pathlib import Path # 파일 경로 처리
import streamlit as st # 웹 기반 사용자 인터페이스
from dotenv import load_dotenv


BASE = Path(__file__).resolve().parent

load_dotenv() # .env 로딩

SQL_DIR = Path(__file__).resolve().parent

# 입력 파일 최대 크기를 10Mbytes로 제한
MAX_BYTES = 10 * 1024 * 1024

# 실행 이력을 저장할 jsonl파일 경로
LOG_PATH = Path(__file__).with_name("voice_runs_campus.jsonl")

# 사용할 Whisper모델 크기
MODEL_SIZE = 'base'

# Edge TTS에서 사용하는 한국어 여성 음성 모델 ID
VOICE_ID = 'ko-KR-SunHiNeural'

FACILITIES = {
    "도서관": "도서관은 본관 옆 건물이며 평일 9시부터 18시까지 운영합니다.",
    "학생식당": "학생식당은 학생회관 1층에 있으며 11시부터 14시까지 운영합니다.",
    "취업지원센터": "취업지원센터는 학생회관 2층이며 취업 상담을 제공합니다.",
    "행정실": "행정실은 본관 1층이며 각종 증명서 발급을 안내합니다.",
}

# [구현 절차]
# ② 시설 정보는 파이썬 딕셔너리로 정의합니다(운영시간은 연습용 예시).
# ③ 답변 생성 함수를 만들고 시설명이 들어간 질문에 대응합니다.
# ④ 인식되지 않은 질문은 선택 가능한 시설 목록을 안내합니다.
# ⑤ 음성 녹음 여부 및 10MBytes 용량 제한을 검사합니다.
# ⑥ 모델 실행 중 오류가 발생하면 원인을 표시하되 앱이 중단되지 않도록 합니다.
# ⑦ 각 단계 수행 시간을 perf_counter()로 측정합니다.
# ⑧ 요청 ID, 시각, 사용 모델, 오류 여부, 처리시간을 JSONL로 기록합니다.
# ⑨ Streamlit에서 음성 결과, 구간별 소요시간, 최근 이력을 확인합니다.

def load_sql(filename):
    path = (SQL_DIR / filename).resolve() # SQL파일의 서버상의 실제경로
    # 생성된 sql디렉토리에 SQL파일이 없거나 SQL파일의 확장자가 sql이 아니라면
    if path.parent != SQL_DIR.resolve() or path.suffix != '.sql':
        raise ValueError('sql폴더의 .sql파일명만 지정하세요')
    return path.read_text(encoding='utf-8')


def validate_wav(audio):

    try:

        with wave.open(io.BytesIO(audio), 'rb') as wav:
            channels = wav.getnchannels()
            sample_rate = wav.getframerate()
            sample_width = wav.getsampwidth()
            frames = wav.getnframes()
            compression = wav.getcomptype()

    except (wave.Error, EOFError, OSError) as e:
        raise ValueError('유효한 비압축 PCM WAV 파일을 입력해 주세요') from e

    if compression != 'NONE' or channels not in (1, 2) or sample_rate <= 0 or frames <= 0:
        raise ValueError('모노/스테레오 비압축 PCM WAV 파일이 필요합니다')

    return {
        'channels': channels,
        'sample_rate': sample_rate,
        'bit_depth': sample_width * 8,
        'duration_sec': round(frames / sample_rate, 2)
    }

@st.cache_resource(show_spinner='Whisper 모델을 로딩 중...')
def load_stt_model(model_size):
    return WhisperModel(model_size, device='CPU', compute_type='int8')

def transcript_wav(audio, model_size):

    model = load_stt_model(model_size)

    with tempfile.NamedTemporaryFile(suffix='wav', delete=False) as f:
        f.write(audio)
        wav_path = Path(f.name)

    try:
        segments, info = model.transcribe(
            str(wav_path),
            language='ko',
            vad_filter=True
        )

        transcript = ''.join(segment.text.strip() for segment in segments).strip()

        return transcript

    finally:
        wav_path.unlink(missing_ok=True)

def build_reply(transcript):
    clean = ''.join(transcript.split())
    if not clean:
        return '음성 인식을 하지 못했습니다'

    for name, info in FACILITIES.items():
        if name in clean:
            return info

    facilities = ','.join(FACILITIES.keys())
    return f'안내 가능한 시설은 {facilities}입니다. 시설명을 확인해 주세요'

async def generate_tts_mp3(text, voice_id):
    with tempfile.NamedTemporaryFile(suffix='mp3', delete=False) as f:
        mp3_path = Path(f.name)
    try:
        await edge_tts.Communicate(text=text, voice=voice_id).save(str(mp3_path))
        return mp3_path.read_bytes()
    finally:
        mp3_path.unlink(missing_ok=True)

def synthesize_speech(text, voice_id):
    mp3_bytes = asyncio.run(generate_tts_mp3(text, voice_id))
    return mp3_bytes, 'audio/mpeg'

def store_meta(meta):

    with LOG_PATH.open('a', encoding='utf-8-sig') as f:
        f.write(json.dumps(meta, ensure_ascii=False) + '\n')

    database_url = os.getenv('DATABASE_URL', '').strip()

    if not database_url:
        return

    try:
        with psycopg.connect(database_url, connect_timeout=5) as conn:
            with conn.cursor() as cur:
                cur.execute(load_sql('create_table.sql'))
                cur.execute(load_sql('insert_table.sql'),
                    (
                        meta['request_id'], meta['created_at'], meta['status'],
                        meta['stt_ms'], meta['reply_ms'], meta['tts_ms'],
                        meta['total_ms'], meta['model_version'], meta['audio_sha256']
                    )
                )

    except Exception as e:
        st.warning(f'DB 실행 이력 저장 실패: {type(e).__name__}: {e}')

def run_pipeline(audio, model_size, voice_id):

    request_id = str(uuid.uuid4())

    digest = hashlib.sha256(audio).hexdigest()

    marks = {'stt_ms': 0.0, 'reply_ms': 0.0, 'tts_ms': 0.0, 'total_ms': 0.0}

    started = time.perf_counter()

    result = None
    error = None

    try:
        t0 = time.perf_counter()
        transcript = transcript_wav(audio, model_size)
        marks['stt_ms'] = (time.perf_counter() - t0) * 1000

        t0 = time.perf_counter()
        reply = build_reply(transcript)
        marks['reply_ms'] = (time.perf_counter() - t0) * 1000

        t0 = time.perf_counter()
        output, mime = synthesize_speech(reply, voice_id)
        marks['tts_ms'] = (time.perf_counter() - t0) * 1000

        marks['total_ms'] = (time.perf_counter() - started) * 1000

        result = (transcript, reply, output, mime)

    except Exception as e:
        error = e

    elapsed = (time.perf_counter() - started) * 1000

    meta = {
        'request_id': request_id,
        'created_at': datetime.now(timezone.utc).isoformat(),
        'status': 'error' if error else 'success',
        **{key: round(value, 2) for key, value in marks.items()},
        'total_ms': round(elapsed, 2),
        'model_version': f'whisper={model_size}/edge-tts/{voice_id}',
        'audio_sha256': digest
    }

    store_meta(meta)

    return result, marks, meta, error

def main():
    st.set_page_config(
        page_title='캠퍼스 시설 안내 음성 도우미',
        layout = 'centered'
    )

    st.title('캠퍼스 시설 안내 음성 도우미')

    with st.form('voice_form'):

        source = st.radio('음성 입력 방식', ['WAV파일 업로드', '마이크 녹음'])

        if source == 'WAV파일 업로드':
            uploaded = st.file_uploader('실제 WAV파일을 선택하세요', type=['wav'])
        else:
            uploaded = st.audio_input('마이크로 녹음하세요')

        model_size = st.selectbox(
            'STT모델',
            ['tiny', 'base', 'small', 'medium'],
            index=1
        )

        voice_id = st.selectbox(
            'TTS 목소리',
            ['ko-KR-SunHiNeural', 'ko-KR-InJoonNeural']
        )

        submitted = st.form_submit_button('음성 파이프라인 실행')

    if not submitted:
        return

    if uploaded is None:
        st.error('WAV파일을 업로드하거나 녹음해주세요')
        return

    audio = uploaded.getvalue()

    if not audio or len(audio) > MAX_BYTES:
        st.error(f'WAV파일은 0바이트보다 크고 {MAX_BYTES}바이트 보다 작아야합니다')
        return

    try:
        audio_info = validate_wav(audio)
    except ValueError as e:
        st.error(str(e))
        return

    st.subheader('입력 WAV 파일')

    st.audio(audio, format='audio/wav')

    st.json(audio_info)

    with st.spinner('처리 중...'):
        result, marks, meta, error = run_pipeline(audio, model_size, voice_id)

    if error:
        st.error(f'처리오류{type(error).__name__}: {error}')
    else:

        transcript, reply, output, mime = result

        st.subheader('STT인식 결과')
        st.write(transcript or '인식된 음성이 없습니다')

        st.subheader('시설 안내')
        st.write(reply)

        st.subheader('TTS 합성 음성')
        st.audio(output, format=mime)

        st.download_button('TTS MP3 저장', output, 'reply.mp3', mime=mime)

    st.metric('총 처리시간', f'{meta['total_ms']:.0f} ms')

    st.bar_chart(marks)

    st.json(meta)


if __name__ == '__main__':
    main()

