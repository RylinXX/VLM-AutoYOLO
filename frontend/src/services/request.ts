import axios from "axios";

export const DETECT_TIMEOUT = 600_000;       // 10 min — ML inference can be slow
export const UPLOAD_TIMEOUT = 300_000;       // 5 min — chunked uploads
export const DEFAULT_TIMEOUT = 60_000;        // 1 min — list/rename/cancel etc.

export const request = axios.create({
  baseURL: API_BASE,
  timeout: DEFAULT_TIMEOUT,
});

// Axios response interceptor to extract error details uniformly
request.interceptors.response.use(
  (response) => response,
  (error) => {
    // Backend error envelope is {error: {code, message}}; fall back to legacy fields.
    const data = error.response?.data;
    const message =
      data?.error?.message ||
      (typeof data?.detail === "string" ? data.detail : undefined) ||
      data?.message ||
      error.message ||
      "Request failed";
    return Promise.reject(new Error(message));
  },
);
