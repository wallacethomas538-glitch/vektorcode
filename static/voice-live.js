/* VektorCode Live Voice — browser client (experiment only).
 *
 * Flow: tap Start -> POST /api/voice/token (mints a LiveKit token and
 * dispatches the voice worker into a fresh room) -> join the room ->
 * publish the microphone (echo cancellation, noise suppression, auto
 * gain on) -> play the agent's audio as it arrives.
 *
 * The heavy work (VAD, turn detection, STT, brain, TTS, barge-in)
 * runs in the voice worker, not here. This page only moves audio
 * and shows state. The old mic button (browser SpeechRecognition,
 * continuous=false) is untouched and still works for typed chat.
 */
(function () {
    'use strict';
    var stateEl = document.getElementById('state');
    var logEl = document.getElementById('log');
    var startBtn = document.getElementById('start-btn');
    var stopBtn = document.getElementById('stop-btn');
    var dispatchNote = document.getElementById('dispatch-note');
    var room = null;
    var micTrack = null;

    function setState(text, cls) {
        stateEl.textContent = text;
        stateEl.className = cls || '';
    }
    function log(text) {
        logEl.textContent += text + '\n';
        logEl.scrollTop = logEl.scrollHeight;
    }
    function showError(text) {
        setState('Error — ' + text, 'error');
        log('ERROR: ' + text);
    }

    async function start() {
        startBtn.disabled = true;
        setState('Connecting…');
        log('Requesting a voice token…');
        var res;
        try {
            res = await fetch('/api/voice/token', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({}),
            });
        } catch (err) {
            showError('Could not reach the server: ' + err.message);
            startBtn.disabled = false;
            return;
        }
        var data = await res.json().catch(function () { return {}; });
        if (!res.ok || !data.ok) {
            showError(data.error || ('Voice token failed (HTTP ' + res.status + ')'));
            startBtn.disabled = false;
            return;
        }
        if (data.agent_dispatch && data.agent_dispatch !== 'ok') {
            dispatchNote.textContent = 'Warning: the voice worker did not join the room (' + data.agent_dispatch + '). You can still connect, but nobody will answer.';
            log('WARNING: agent dispatch: ' + data.agent_dispatch);
        } else {
            dispatchNote.textContent = '';
        }
        log('Joined room ' + data.room + '.');

        try {
            room = new LivekitClient.Room({ adaptiveStream: true, dynacast: true });
        } catch (err) {
            showError('LiveKit client failed to load: ' + err.message);
            startBtn.disabled = false;
            return;
        }

        room.on(LivekitClient.RoomEvent.TrackSubscribed, function (track, publication, participant) {
            if (track.kind === LivekitClient.Track.Kind.Audio) {
                var el = track.attach();
                el.setAttribute('playsinline', '');
                document.getElementById('remote-audio').appendChild(el);
                el.play().catch(function () { /* autoplay needs a tap; Start was the tap */ });
                log('Agent audio connected.');
            }
        });
        room.on(LivekitClient.RoomEvent.TrackUnsubscribed, function (track) {
            track.detach().forEach(function (el) { el.remove(); });
        });
        room.on(LivekitClient.RoomEvent.ActiveSpeakersChanged, function (speakers) {
            var agentSpeaking = speakers.some(function (p) { return p.isAgent; });
            var youSpeaking = speakers.some(function (p) { return p.isLocal; });
            if (agentSpeaking) setState('Agent is speaking — interrupt any time', 'speaking');
            else if (youSpeaking) setState('Listening to you…', 'listening');
            else setState('Listening — speak when ready', 'listening');
        });
        room.on(LivekitClient.RoomEvent.TranscriptionReceived, function (segments) {
            segments.forEach(function (seg) {
                if (seg.final) log((seg.participantIdentity || 'voice') + ': ' + seg.text);
            });
        });
        room.on(LivekitClient.RoomEvent.Disconnected, function () {
            setState('Disconnected');
            startBtn.disabled = false;
            stopBtn.disabled = true;
        });

        try {
            await room.connect(data.url, data.token);
        } catch (err) {
            showError('Could not join the voice room: ' + err.message);
            startBtn.disabled = false;
            return;
        }

        try {
            micTrack = await LivekitClient.createLocalAudioTrack({
                echoCancellation: true,
                noiseSuppression: true,
                autoGainControl: true,
            });
        } catch (err) {
            showError('Microphone blocked — allow the mic in Chrome, then tap Start again. (' + (err.name || err.message) + ')');
            room.disconnect();
            startBtn.disabled = false;
            return;
        }
        await room.localParticipant.publishTrack(micTrack);
        setState('Listening — speak when ready', 'listening');
        log('Microphone live. Speak, pause naturally, interrupt the agent any time.');
        stopBtn.disabled = false;
    }

    async function stop() {
        stopBtn.disabled = true;
        try { if (micTrack) { micTrack.stop(); micTrack = null; } } catch (_) {}
        try { if (room) { await room.disconnect(); room = null; } } catch (_) {}
        setState('Stopped');
        startBtn.disabled = false;
        log('Voice session ended.');
    }

    startBtn.addEventListener('click', start);
    stopBtn.addEventListener('click', stop);
})();
