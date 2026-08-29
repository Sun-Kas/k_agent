import { chmodSync } from "node:fs";

// npm 的 bin 入口必须可执行；TypeScript 新生成的文件默认不保留源文件权限。
chmodSync(new URL("../dist/index.js", import.meta.url), 0o755);
