import assert from "node:assert/strict";
import { EventEmitter } from "node:events";
import test from "node:test";
import { decideServiceAction, stopOwnedProcess } from "../scripts/dev-local.mjs";

test("dev:local 复用已经健康的 K Agent 服务", () => {
  assert.equal(decideServiceAction({ healthy: true, portOpen: true }, "Access Layer"), "reuse");
});

test("dev:local 只在端口空闲且服务不健康时启动", () => {
  assert.equal(decideServiceAction({ healthy: false, portOpen: false }, "Agent Backend"), "start");
});

test("dev:local 拒绝占用目标端口的非预期服务", () => {
  assert.throws(
    () => decideServiceAction({ healthy: false, portOpen: true }, "Access Layer"),
    /端口已被占用/,
  );
});

test("dev:local 退出时先优雅终止自己拥有的服务", async () => {
  const child = new FakeChild(true);
  await stopOwnedProcess(child, 20);
  assert.deepEqual(child.signals, ["SIGTERM"]);
});

test("dev:local 优雅终止超时后强制回收服务", async () => {
  const child = new FakeChild(false);
  await stopOwnedProcess(child, 5);
  assert.deepEqual(child.signals, ["SIGTERM", "SIGKILL"]);
});

class FakeChild extends EventEmitter {
  exitCode: number | null = null;
  signalCode: NodeJS.Signals | null = null;
  readonly signals: NodeJS.Signals[] = [];

  constructor(private readonly terminateOnSigterm: boolean) {
    super();
  }

  kill(signal: NodeJS.Signals): boolean {
    this.signals.push(signal);
    if ((signal === "SIGTERM" && this.terminateOnSigterm) || signal === "SIGKILL") {
      queueMicrotask(() => {
        this.signalCode = signal;
        this.emit("exit", null, signal);
      });
    }
    return true;
  }
}
