export const EXIT_CODES = {
  success: 0,
  agentError: 1,
  inputRequired: 2,
  connectionError: 3,
  configError: 4,
  interrupted: 130,
} as const;

export const EXIT_CODE = EXIT_CODES;
