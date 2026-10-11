// Browser-side copies of auth-service's input rules
// (services/auth-service/domains/auth/schema.py, config.py). They give quick
// feedback before submitting; the backend still checks everything.

export const USERNAME_PATTERN = "[A-Za-z0-9_.\\-]{3,32}";
export const USERNAME_TITLE = "3 to 32 characters: letters, digits, dot (.), underscore (_) or hyphen (-).";

export const PASSWORD_MIN = 10;
// bcrypt only reads the first 72 bytes, so the backend refuses longer
// passwords. maxLength counts characters, not bytes, so the byte limit
// is checked separately below.
export const PASSWORD_MAX_BYTES = 72;
export const PASSWORD_TITLE = `At least ${PASSWORD_MIN} characters and at most ${PASSWORD_MAX_BYTES} bytes (accented letters and emoji count as more than one byte).`;

// Generous standard limit for an email address.
export const EMAIL_MAX = 254;

// For an input's onChange: marks the field invalid when the password is
// too long in bytes, so the browser blocks the submit with this message.
export function checkPasswordBytes(input: HTMLInputElement): void {
  const tooLong = new TextEncoder().encode(input.value).length > PASSWORD_MAX_BYTES;
  input.setCustomValidity(tooLong ? `Password must be at most ${PASSWORD_MAX_BYTES} bytes.` : "");
}
