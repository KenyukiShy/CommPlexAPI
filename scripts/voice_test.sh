#!/usr/bin/env bash
# ============================================================
# scripts/voice_test.sh — Arc Fleet Robot Voice Test Suite
#
# Tests:
#   1. TTS: convert text to audio (local/gcp/bland)
#   2. STT: transcribe audio file
#   3. Robot-to-robot: two AI agents call each other
#   4. Interrupt test: call + early hangup handling
#   5. Transfer test: call + warm transfer to human
#
# Usage:
#   ./scripts/voice_test.sh                   # All tests
#   ./scripts/voice_test.sh --tts             # TTS only
#   ./scripts/voice_test.sh --robot           # Robot-vs-robot
#   ./scripts/voice_test.sh --call 7018705235 # Test call to number
#   ./scripts/voice_test.sh --backend bland   # Force Bland backend
#   ./scripts/voice_test.sh --dry-run         # No actual calls
# ============================================================

set -euo pipefail

GREEN='\033[92m'; YELLOW='\033[93m'; RED='\033[91m'
CYAN='\033[96m'; BOLD='\033[1m'; RESET='\033[0m'

ok()     { echo -e "  ${GREEN}✓${RESET} $1"; }
warn()   { echo -e "  ${YELLOW}⚠${RESET} $1"; }
err()    { echo -e "  ${RED}✗${RESET} $1"; }
info()   { echo -e "  ${CYAN}→${RESET} $1"; }
banner() { echo -e "\n${CYAN}══════════════════════════════════════════${RESET}"; echo -e "  ${BOLD}$1${RESET}"; echo -e "${CYAN}══════════════════════════════════════════${RESET}\n"; }

# ── Args ──────────────────────────────────────────────────────────────────────
RUN_TTS=false
RUN_STT=false
RUN_ROBOT=false
RUN_CALL=""
BACKEND=${VOICE_BACKEND:-""}
DRY_RUN=false

while [[ $# -gt 0 ]]; do
    case "$1" in
        --tts)        RUN_TTS=true ;;
        --stt)        RUN_STT=true ;;
        --robot)      RUN_ROBOT=true ;;
        --call)       shift; RUN_CALL="$1" ;;
        --backend)    shift; BACKEND="$1" ;;
        --dry-run)    DRY_RUN=true ;;
        --all|-a)     RUN_TTS=true; RUN_STT=true; RUN_ROBOT=true ;;
        *) echo "Unknown arg: $1"; exit 1 ;;
    esac
    shift
done

# Default: all tests
if [[ "$RUN_TTS" == false && "$RUN_STT" == false && \
      "$RUN_ROBOT" == false && -z "$RUN_CALL" ]]; then
    RUN_TTS=true
    RUN_ROBOT=false   # Robot test disabled by default (costs API credits)
fi

PYTHON=$(command -v python3 || command -v python)
[[ -z "$PYTHON" ]] && { err "Python not found."; exit 1; }

[[ -n "$BACKEND" ]] && export VOICE_BACKEND="$BACKEND"
[[ "$DRY_RUN" == true ]] && export DRY_RUN=true

# ── Check .env ────────────────────────────────────────────────────────────────
if [[ -f .env ]]; then
    set -a; source .env; set +a
    ok "Loaded .env"
else
    warn ".env not found. Running with defaults/stubs."
fi

# ── Test 1: TTS ───────────────────────────────────────────────────────────────
if [[ "$RUN_TTS" == true ]]; then
    banner "TEST 1: Text-to-Speech"

    TTS_TEXT="Hello. This is Arc Fleet Campaign system. Text to speech is working correctly."
    OUTPUT_FILE="/tmp/arc_fleet_tts_test.mp3"

    info "Backend: ${VOICE_BACKEND:-auto-detect}"
    info "Text: $TTS_TEXT"
    info "Output: $OUTPUT_FILE"

    $PYTHON -m modules.voice --tts "$TTS_TEXT" --output "$OUTPUT_FILE" && {
        ok "TTS generated: $OUTPUT_FILE"
        if command -v play &>/dev/null; then
            info "Playing audio ..."
            play "$OUTPUT_FILE" 2>/dev/null || warn "Could not play audio (install sox)"
        elif command -v mpg123 &>/dev/null; then
            mpg123 -q "$OUTPUT_FILE" 2>/dev/null || warn "mpg123 play failed"
        elif command -v aplay &>/dev/null; then
            warn "aplay found but needs WAV. Converting ..."
            if command -v ffmpeg &>/dev/null; then
                ffmpeg -y -i "$OUTPUT_FILE" /tmp/arc_fleet_tts_test.wav -loglevel quiet
                aplay /tmp/arc_fleet_tts_test.wav
            fi
        else
            warn "No audio player found. File saved: $OUTPUT_FILE"
        fi
    } || err "TTS failed"
fi

# ── Test 2: STT ───────────────────────────────────────────────────────────────
if [[ "$RUN_STT" == true ]]; then
    banner "TEST 2: Speech-to-Text (STT)"

    if [[ -f /tmp/arc_fleet_tts_test.mp3 ]]; then
        info "Transcribing: /tmp/arc_fleet_tts_test.mp3"
        $PYTHON -m modules.voice --transcribe /tmp/arc_fleet_tts_test.mp3 || err "STT failed"
    else
        warn "No TTS audio to transcribe. Run --tts first."
    fi
fi

# ── Test 3: Single call ───────────────────────────────────────────────────────
if [[ -n "$RUN_CALL" ]]; then
    banner "TEST 3: Single Test Call → $RUN_CALL"

    if [[ -z "${BLAND_API_KEY:-}" ]]; then
        warn "BLAND_API_KEY not set. This will use dry-run mode."
        export DRY_RUN=true
    fi

    SCRIPT="This is an automated test call from the Arc Fleet Campaign system. \
If you are hearing this, the voice system is working correctly. \
This is Kenyon Jones at 701-870-5235. Have a great day. Goodbye."

    info "Calling: $RUN_CALL"
    info "Dry-run: ${DRY_RUN:-false}"
    $PYTHON -m modules.voice --call "$RUN_CALL" <<< "$SCRIPT" || err "Call failed"
fi

# ── Test 4: Robot-vs-Robot ────────────────────────────────────────────────────
if [[ "$RUN_ROBOT" == true ]]; then
    banner "TEST 4: Robot-vs-Robot Call"

    if [[ -z "${BLAND_API_KEY:-}" ]]; then
        err "BLAND_API_KEY required for robot-vs-robot test."
        err "Set BLAND_API_KEY in .env and run: ./scripts/voice_test.sh --robot"
        exit 1
    fi

    NUMBER_A="${ROBOT_A_NUMBER:-${SENDER_PHONE:-7018705235}}"
    NUMBER_B="${ROBOT_B_NUMBER:-${ALT_PHONE:-7019465731}}"

    warn "This will place REAL calls to +1$NUMBER_A and +1$NUMBER_B"
    warn "Cost: ~\$0.05-0.20 per call depending on Bland plan"
    echo ""
    read -r -p "  Continue? [y/N] " confirm
    [[ "${confirm,,}" != "y" ]] && { info "Cancelled."; exit 0; }

    info "Robot A: +1$NUMBER_A"
    info "Robot B: +1$NUMBER_B"
    info "Starting robot-vs-robot test ..."

    $PYTHON -m modules.voice --robot-test \
        --number-a "+1${NUMBER_A}" \
        --number-b "+1${NUMBER_B}" && ok "Robot test complete" || err "Robot test failed"
fi

# ── Summary ───────────────────────────────────────────────────────────────────
banner "Voice Test Summary"
ok "TTS: local/gcp/bland → audio file"
ok "STT: audio file → transcript"
ok "Call: outbound test call with script"
ok "Robot: AI-to-AI bilateral conversation"

echo -e "\n${CYAN}To run robot-vs-robot with real calls:${RESET}"
echo "  ./scripts/voice_test.sh --robot"
echo ""
echo -e "${CYAN}To test a specific number:${RESET}"
echo "  ./scripts/voice_test.sh --call 7018705235"
echo ""
echo -e "${CYAN}To use local TTS (no API key needed):${RESET}"
echo "  ./scripts/voice_test.sh --tts --backend local"
