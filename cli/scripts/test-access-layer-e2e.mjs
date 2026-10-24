import assert from 'node:assert/strict';
import { readFile, writeFile } from 'node:fs/promises';
import { join } from 'node:path';
import { EventEmitter } from 'node:events';
import { AccessLayerClient } from '../dist/client/access-layer-client.js';
import { SessionPtyHub } from '../dist/application/session-pty.js';

// 仅消费本任务的一次性 /private/tmp fixture，禁止指向用户正式数据或远端服务。
const fixture = JSON.parse(await readFile(process.argv[2] ?? '/private/tmp/k-agent-cli-api-fixture.json', 'utf8'));
assert.match(fixture.repo, /^\/private\/tmp\/k-agent-cli-api-[^/]+\/repo$/);
assert.equal(new URL(fixture.endpoint).hostname, '127.0.0.1');
const client = new AccessLayerClient(fixture.endpoint);
const project = await client.createProject(fixture.repo, 'CLI 集成测试');
await client.updateProject(project.id, 'CLI 集成测试', 'main');
const local = await client.createProjectSession(project.id, 'local');
const isolated = await client.createProjectSession(project.id, 'worktree');
assert.equal(local.environment.cwd, fixture.repo);
assert.notEqual(isolated.environment.cwd, fixture.repo);
assert.equal((await client.sessionEnvironment(isolated.id)).environment.kind, 'worktree');
await writeFile(join(isolated.environment.cwd, 'cli-change.txt'), 'created through fixture\n');
assert.ok((await client.sessionChanges(isolated.id)).files.some(file => file.path === 'cli-change.txt'));
assert.match(await client.sessionDiff(isolated.id, 'cli-change.txt'), /created through fixture/);
assert.equal((await client.workspaceFile(isolated.id, 'cli-change.txt')).content, 'created through fixture\n');
await client.commitWorkspace(isolated.id, 'fixture CLI change');
assert.equal((await client.runIntegration(isolated.id, 'start')).status, 'completed');
assert.equal(await readFile(join(fixture.repo, 'cli-change.txt'), 'utf8'), 'created through fixture\n');
const forked = await client.forkSession(local.id, { environment: 'same-directory' });
assert.equal(forked.environment.cwd, fixture.repo);
await client.handoffSession(isolated.id, 'local');
assert.equal((await client.sessionEnvironment(isolated.id)).environment.cwd, fixture.repo);

const taskInput = { name: '未来测试任务', prompt: '不会执行', scheduleKind: 'once', localDate: '2999-01-01', weekdays: [], localTime: '09:00', timezone: 'Asia/Shanghai', agentKind: 'k_agent', modelId: 'fixture', reasoningEffort: 'none', mcpServerIds: [], skillIds: [], permissionMode: 'default' };
const task = await client.createScheduledTask(taskInput);
await client.updateScheduledTask(task.id, { ...taskInput, name: '已编辑测试任务' });
assert.equal((await client.getScheduledTask(task.id)).name, '已编辑测试任务');
await client.scheduledTaskAction(task.id, 'pause');
assert.equal((await client.getScheduledTask(task.id)).status, 'paused');
await client.scheduledTaskAction(task.id, 'resume');
assert.deepEqual(await client.listScheduledRuns(task.id), []);
await client.deleteScheduledTask(task.id);

const team = await client.createTeam({ name: 'CLI 测试团队', goal: '检查持久记录', mode: 'manual', maxParallel: 1, permissionMode: 'default', agents: [{ name: '主管', role: 'supervisor', agentKind: 'k_agent', modelId: 'fixture', isSupervisor: true, mcpServerIds: [], skillIds: [] }] });
const snapshot = await client.getTeam(team.id);
assert.equal(snapshot.name, 'CLI 测试团队');
assert.ok((await client.teamEvents(team.id)).length > 0);
await client.sendTeamMessage(team.id, snapshot.agents[0].id, '仅测试消息记录');
await client.commandTeam(team.id, 'cancel');
assert.equal((await client.getTeam(team.id)).status, 'cancelled');

class Input extends EventEmitter { isTTY = true; isRaw = false; setRawMode(value) { this.isRaw = value; } resume() {} }
class Output extends EventEmitter { isTTY = true; columns = 91; rows = 31; data = []; write(value) { this.data.push(Buffer.from(value)); return true; } }
const stdin = new Input(); const stdout = new Output();
const hub = new SessionPtyHub(fixture.endpoint);
const attached = hub.attach(local.id, 'cli-test-native', { stdin, stdout });
try {
  await new Promise(resolve => setTimeout(resolve, 500));
  stdin.emit('data', Buffer.from('printf "\\033[2J\\033[3;4HCLI_VT_OK\\n"; pwd\r'));
  for (let i = 0; i < 100; i++) {
    const text = Buffer.concat(stdout.data).toString('utf8');
    if (text.includes('\x1b[3;4HCLI_VT_OK') && text.includes(fixture.repo)) break;
    await new Promise(resolve => setTimeout(resolve, 100));
  }
  const text = Buffer.concat(stdout.data).toString('utf8');
  assert.ok(text.includes('\x1b[3;4HCLI_VT_OK'), 'VT 光标控制序列未完整到达宿主终端');
  assert.ok(text.includes(fixture.repo), 'PTY 没有使用会话 cwd');
  stdin.emit('data', Buffer.from([0x1d])); await attached;
  assert.equal(stdin.isRaw, false);
} finally { hub.closeAll(); }
await client.deleteSession(forked.id); await client.deleteSession(isolated.id); await client.deleteSession(local.id); await client.deleteProject(project.id);
console.log('CLI real HTTP + PTY passed: Project, Local/Worktree cwd, changes/diff/file, commit/integration, fork/handoff, scheduled CRUD/state, team CRUD/message/state, raw VT and terminal restoration. No model or microphone used.');
