export const BYTE_MULTIPLIERS = {
  B: 1,
  KB: 1024,
  MB: 1024 ** 2,
  GB: 1024 ** 3,
  TB: 1024 ** 4,
} as const;

export type ByteUnit = keyof typeof BYTE_MULTIPLIERS;

const BYTE_UNITS_DESCENDING: ByteUnit[] = ["TB", "GB", "MB", "KB", "B"];

const numericBytes = (value: unknown): number | null =>
  typeof value === "number" && Number.isFinite(value) && value >= 0
    ? value
    : null;

export const inferByteUnit = (value: unknown): ByteUnit => {
  const bytes = numericBytes(value);
  if (bytes === null || bytes === 0) return "GB";
  return (
    BYTE_UNITS_DESCENDING.find((unit) => bytes >= BYTE_MULTIPLIERS[unit]) ?? "B"
  );
};

export const byteAmountText = (value: unknown, unit: ByteUnit): string => {
  const bytes = numericBytes(value);
  if (bytes === null) return "";
  return String(Number((bytes / BYTE_MULTIPLIERS[unit]).toFixed(4)));
};

export const parseByteAmount = (raw: string, unit: ByteUnit): number | null => {
  if (raw.trim() === "") return null;
  const amount = Number(raw);
  if (!Number.isFinite(amount) || amount < 0) return null;
  return Math.round(amount * BYTE_MULTIPLIERS[unit]);
};
