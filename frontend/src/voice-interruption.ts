function normalizeSpokenText(value: string) {
  return value.toLocaleLowerCase().replace(/[\s\p{P}\p{S}]+/gu, "");
}

export function shouldInterruptForTranscript(transcript: string, assistantSpeech: string) {
  const candidate = normalizeSpokenText(transcript);
  if (candidate.length < 2) return false;
  const assistant = normalizeSpokenText(assistantSpeech);
  if (!assistant) return true;
  // SpeechRecognition can hear text currently emitted by speechSynthesis.
  // Ignore exact assistant fragments, but interrupt as soon as the recognition
  // result contains words that do not belong to the active utterance.
  return !assistant.includes(candidate);
}
