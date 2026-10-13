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

// Authenticator codes are 6 digits (security.py match_totp_step).
export const TOTP_CODE_PATTERN = "[0-9]{6}";
export const TOTP_CODE_TITLE = "The 6-digit code from your authenticator app.";

// Avatar rules (config.py AVATAR_MAX_SIZE_MB, AVATAR_ALLOWED_CONTENT_TYPES).
// The browser check also keeps files away from Next's 6MB Server Action
// body limit, which would otherwise fail the whole request.
export const AVATAR_TYPES = ["image/png", "image/jpeg", "image/webp", "image/gif"];
const AVATAR_MAX_BYTES = 5 * 1024 * 1024;

export function avatarFileError(file: { size: number; type: string }): string | null {
  if (!AVATAR_TYPES.includes(file.type)) return "Use a PNG, JPEG, WebP or GIF image.";
  if (file.size > AVATAR_MAX_BYTES) return "The image must be 5MB or smaller.";
  return null;
}
