import http from "k6/http";
import { sleep } from "k6";

export const options = {
  vus: 30,
  duration: "3m",
};

export default function () {
  // Sticky key makes canary deterministic per "user"
  const key = `user-${__VU}`;
  const params = { headers: { "X-Sticky-Key": key } };
  http.get("http://localhost:8080/work", params);
  sleep(0.1);
}
