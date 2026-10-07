"""一次 Run 唯一的、深层不可变的工具定义与请求绑定快照。"""

from __future__ import annotations
import re
from dataclasses import dataclass, field
from types import MappingProxyType
from collections.abc import Mapping, Iterable
from jsonschema import Draft202012Validator
from backend.tools.contracts import ToolBinding, ToolKey, thaw
from backend.tools.catalog import ToolCapability, ToolCapabilityView


@dataclass(frozen=True, slots=True)
class RequestToolSet:
    bindings: tuple[ToolBinding, ...]
    # create() 不传入；由 __post_init__ 建成只读名字索引。
    by_provider_name: Mapping[str, ToolBinding] = field(init=False)

    def __post_init__(self):
        # dataclass 填完 bindings 后立刻跑：验完再冻索引。frozen 实例要用 object.__setattr__。
        bindings = tuple(self.bindings)
        names = {}
        keys = set()
        for binding in bindings:
            spec = binding.spec
            expected = spec.key.name if spec.key.source == "local" else f"mcp__{spec.key.namespace}__{spec.key.name}"
            if spec.key.source not in {"local", "mcp"} or spec.provider_name != expected:
                raise ValueError(f"Tool key/provider name mismatch: {spec.provider_name}")
            if (spec.key.source == "local" and spec.key.namespace is not None) or (spec.key.source == "mcp" and not spec.key.namespace):
                raise ValueError(f"Invalid tool namespace: {spec.provider_name}")
            if spec.provider_name in names or spec.key in keys:
                raise ValueError(f"Duplicate tool: {spec.provider_name}")
            if not re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", spec.provider_name):
                raise ValueError(f"Invalid provider tool name: {spec.provider_name}")
            schema = thaw(spec.input_schema)
            if schema.get("type") != "object":
                raise ValueError(
                    f"Tool schema must have object root: {spec.provider_name}"
                )
            Draft202012Validator.check_schema(schema)
            names[spec.provider_name] = binding
            keys.add(spec.key)
        # 技术上，任何实例方法里都可以调用 object.__setattr__() 修改 frozen=True dataclass 的属性。
        # 但是这会破坏 frozen 约束，导致对象变成可变状态，这是非常不安全的。
        # 它必须在初始化过程中由 __post_init__ 计算出来，所以这里用 object.__setattr__ 是非常自然的。
        object.__setattr__(self, "bindings", bindings)
        object.__setattr__(self, "by_provider_name", MappingProxyType(names))

    @classmethod
    def create(cls, bindings: Iterable[ToolBinding]) -> RequestToolSet:
        # 入口只收 iterable（list 也行），转成 tuple 再构造，真正校验在 __post_init__。
        return cls(tuple(bindings))

    def resolve(self, provider_name: str) -> ToolBinding:
        try:
            return self.by_provider_name[provider_name]
        except KeyError as exc:
            raise RuntimeError(f"Unknown tool requested: {provider_name}") from exc

    def resolve_key(self, key: ToolKey) -> ToolBinding:
        for binding in self.bindings:
            if binding.spec.key == key:
                return binding
        raise RuntimeError(f"Unknown tool key: {key}")

    def provider_specs(self):
        return [
            {
                "type": "function",
                "function": {
                    "name": binding.spec.provider_name,
                    "description": binding.spec.provider_description,
                    "parameters": thaw(binding.spec.input_schema),
                },
            }
            for binding in self.bindings
        ]

    def capability_view(self):
        return ToolCapabilityView(
            tuple(
                ToolCapability(
                    b.spec.provider_name, b.spec.key.source, b.spec.key.namespace
                )
                for b in self.bindings
            )
        )

    def context_policies(self):
        return MappingProxyType(
            {b.spec.provider_name: b.spec.context_policy for b in self.bindings}
        )


'''
这段代码可以理解成一个**“工具集合 + 工具索引 + 强校验”的不可变容器**。

它做的事情并不只是“把几个 ToolBinding 放进列表里”，而是：

接收一批工具 → 统一转成不可变 tuple → 校验每个工具是否合法 → 建立 provider_name → ToolBinding 的只读索引 → 整个对象创建完成后不能再修改。

我按整体设计 → dataclass → __post_init__ → frozen → slots → MappingProxyType → create → 为什么这样设计来讲。

⸻

1. 先看这个类到底想解决什么问题

假设你的 Agent 有这些工具：

查询账户余额
查询账户消耗
创建广告
修改广告

每个工具可能长这样：

ToolBinding(
    spec=...,
    execute=...,
    permission_subjects=...
)

现在 RequestToolSet 就是把这些工具统一装起来：

tool_set = RequestToolSet.create([
    tool1,
    tool2,
    tool3,
])

然后你可能有两种使用方式。

方式一：遍历所有工具

for binding in tool_set.bindings:
    ...

所以：

bindings: tuple[ToolBinding, ...]

负责保存所有工具。

⸻

方式二：根据工具名字快速找到工具

例如：

tool = tool_set.by_provider_name["query_balance"]

或者 MCP：

tool = tool_set.by_provider_name["mcp__ams__query_balance"]

所以：

by_provider_name: Mapping[str, ToolBinding]

负责提供一个：

工具名字
    ↓
ToolBinding

的索引。

因此整个类可以画成：

RequestToolSet
│
├── bindings
│     ├── ToolBinding 1
│     ├── ToolBinding 2
│     └── ToolBinding 3
│
└── by_provider_name
      ├── "query_balance"       → ToolBinding 1
      ├── "query_cost"          → ToolBinding 2
      └── "mcp__ams__create_ad" → ToolBinding 3

⸻

2. @dataclass(frozen=True, slots=True) 是什么意思？

先拆开：

@dataclass(frozen=True, slots=True)
class RequestToolSet:

相当于同时开启了两个功能：

dataclass
├── frozen=True
└── slots=True

⸻

3. @dataclass 做了什么？

普通 Python 类：

class RequestToolSet:
    def __init__(self, bindings):
        self.bindings = bindings

如果使用：

@dataclass
class RequestToolSet:
    bindings: tuple[ToolBinding, ...]

Python 会自动帮你生成类似：

def __init__(self, bindings):
    self.bindings = bindings

以及：

__repr__
__eq__

等方法。

所以：

@dataclass
class RequestToolSet:
    bindings: tuple[ToolBinding, ...]

主要就是减少模板代码。

⸻

4. frozen=True 是什么？

这个非常重要。

@dataclass(frozen=True)

表示：

实例创建之后，不允许通过普通方式修改字段。

例如：

tool_set = RequestToolSet(...)
tool_set.bindings = (...)

会报错。

类似：

FrozenInstanceError

也就是说：

创建阶段
    ↓
可以设置属性
    ↓
对象创建完成
    ↓
冻结
    ↓
不能再修改属性

这很适合你的 RequestToolSet。

因为工具集合一旦创建：

Request
  ↓
创建 ToolSet
  ↓
校验工具
  ↓
生成索引
  ↓
Agent 执行过程中

通常不希望中途有人偷偷：

tool_set.bindings = new_tools

否则可能出现：

bindings 里面是一套工具
by_provider_name 里面又是另一套工具

产生数据不一致。

⸻

5. 为什么 bindings 要用 tuple？

这里：

bindings: tuple[ToolBinding, ...]

而不是：

bindings: list[ToolBinding]

核心也是为了不可变。

例如：

bindings = [
    tool1,
    tool2,
]

即使 RequestToolSet 是 frozen：

tool_set.bindings.append(tool3)

仍然可能修改里面的 list。

因为：

frozen
只保证：
tool_set.bindings 这个“引用”不能重新赋值
不保证：
bindings 指向的 list 本身不能修改

而 tuple：

bindings = (
    tool1,
    tool2,
)

不能：

bindings.append(tool3)

也不能：

bindings[0] = tool3

所以：

frozen=True
+
tuple

形成了更完整的不可变结构。

⸻

6. slots=True 又是什么？

@dataclass(slots=True)

会给类生成 __slots__。

大致相当于：

class RequestToolSet:
    __slots__ = (
        "bindings",
        "by_provider_name",
    )

普通 Python 对象通常有：

__dict__

例如：

obj.foo = 123
obj.bar = 456

这些属性可以动态塞进 __dict__。

而 slots 限制对象只能拥有声明过的字段。

你的类只允许：

bindings
by_provider_name

这样的属性。

不能随便：

tool_set.abc = 123

这会报错。

所以 slots=True 主要带来：

* 限制动态属性
* 减少对象内存开销
* 对大量创建的小对象比较有价值

⸻

7. 这个字段非常关键

by_provider_name: Mapping[str, ToolBinding] = field(init=False)

这里包含三个概念：

Mapping
field(...)
init=False

分别看。

⸻

8. Mapping[str, ToolBinding] 是什么意思？

它表示：

key: str
value: ToolBinding

也就是：

{
    "query_balance": tool1,
    "query_cost": tool2,
}

但是这里故意没有写：

dict[str, ToolBinding]

而是：

Mapping[str, ToolBinding]

因为它只关心：

这是一个“映射”。

不关心底层一定是不是 dict。

例如：

dict
MappingProxyType
自定义 Mapping

都可以。

这是一种面向接口编程的思想。

⸻

9. field(init=False) 是什么意思？

正常 dataclass：

@dataclass
class A:
    x: int
    y: int

那么：

A(1, 2)

相当于：

A.__init__(x=1, y=2)

但是：

by_provider_name = field(init=False)

告诉 dataclass：

by_provider_name 不要放进 __init__。

因此：

RequestToolSet(
    bindings=...,
)

可以。

但是：

RequestToolSet(
    bindings=...,
    by_provider_name=...
)

不行。

为什么？

因为 by_provider_name 不是用户传进来的原始数据，而是根据 bindings 自动计算出来的派生数据。

也就是：

bindings
    ↓
计算
    ↓
by_provider_name

因此只需要让用户提供：

bindings

然后类自己生成：

by_provider_name

这就是：

field(init=False)

非常典型的使用场景。

⸻

10. 那么 __post_init__ 是什么时候执行？

dataclass 创建对象时，大致过程是：

RequestToolSet(...)
        ↓
dataclass 自动生成 __init__()
        ↓
给 bindings 赋值
        ↓
调用 __post_init__()
        ↓
对象初始化完成

也就是说：

def __post_init__(self):

就是：

dataclass 的 __init__ 执行完之后，自动执行的初始化钩子。

例如：

@dataclass
class User:
    name: str
    age: int
    def __post_init__(self):
        if self.age < 0:
            raise ValueError("年龄不能小于 0")

那么：

User("Tom", -1)

创建过程中就会触发：

__post_init__()

然后抛异常。

⸻

11. 为什么这里大量逻辑都放在 __post_init__？

因为这个类需要保证：

一个 RequestToolSet 一旦创建成功，就一定是一个合法的 ToolSet。

所以它不是简单：

self.bindings = bindings

而是：

bindings
   ↓
规范化
   ↓
检查工具名称
   ↓
检查 namespace
   ↓
检查重复
   ↓
检查 provider_name 格式
   ↓
检查 schema
   ↓
建立索引
   ↓
冻结

这属于一种非常典型的：

构造时校验（construction-time validation）

思想。

⸻

12. 第一行

bindings = tuple(self.bindings)

这一步非常重要。

即使调用者传入：

[
    tool1,
    tool2,
]

这里也马上转换：

tuple(...)

变成：

(
    tool1,
    tool2,
)

所以：

RequestToolSet.create([tool1, tool2])

最终内部不是 list，而是 tuple。

这可以防止外部这样修改：

tools = [tool1, tool2]
tool_set = RequestToolSet.create(tools)
tools.append(tool3)

如果内部直接引用 tools，就会产生外部修改影响内部状态的问题。

但现在：

bindings = tuple(self.bindings)

相当于做了一次快照：

外部 list
   │
   │ tuple()
   ↓
内部 tuple

外部 list 再怎么修改，也不会影响内部 tuple。

⸻

13. 接下来

names = {}
keys = set()

这里准备两个辅助数据结构。

names

names = {}

最终会变成：

{
    "query_balance": binding1,
    "query_cost": binding2,
}

用于：

by_provider_name

⸻

keys

keys = set()

用于检测：

spec.key

是否重复。

也就是说它同时检查两种重复：

provider_name 是否重复
        +
ToolKey 是否重复

⸻

14. 遍历每一个 ToolBinding

for binding in bindings:

假设：

bindings
├── binding1
├── binding2
└── binding3

每次拿一个：

binding

然后：

spec = binding.spec

说明 ToolBinding 里面有：

binding.spec

而 spec 里面又有：

spec.key
spec.provider_name
spec.input_schema

可以理解成：

ToolBinding
│
├── spec
│    ├── key
│    │   ├── source
│    │   ├── namespace
│    │   └── name
│    │
│    ├── provider_name
│    │
│    └── input_schema
│
└── execute

⸻

15. 这一行是在计算“应该叫什么名字”

expected = (
    spec.key.name
    if spec.key.source == "local"
    else f"mcp__{spec.key.namespace}__{spec.key.name}"
)

这是整个代码比较核心的一部分。

它定义了一个规则：

local 工具

如果：

spec.key.source == "local"

那么：

expected = spec.key.name

例如：

source = local
namespace = None
name = query_balance

那么：

expected = query_balance

⸻

MCP 工具

如果：

source = mcp

那么：

expected = f"mcp__{namespace}__{name}"

例如：

source = mcp
namespace = ams
name = query_balance

得到：

mcp__ams__query_balance

所以最终规范就是：

local
    query_balance
MCP
    mcp__ams__query_balance

⸻

16. 为什么要规定这种名字？

因为系统可能同时存在：

local.query_balance
mcp.ams.query_balance
mcp.xxx.query_balance

如果全部只使用：

query_balance

就可能冲突。

因此 MCP 工具把 namespace 编进名字：

mcp__ams__query_balance

这样 provider 层面的工具名字天然具有一定唯一性。

可以理解成：

source + namespace + name
          ↓
provider_name

⸻

17. 这一段是在验证 source 和 provider_name 是否匹配

if spec.key.source not in {"local", "mcp"} or spec.provider_name != expected:
    raise ValueError(
        f"Tool key/provider name mismatch: {spec.provider_name}"
    )

拆开：

spec.key.source not in {"local", "mcp"}

表示：

source 只能是 local 或 mcp。

例如：

source = "abc"

直接非法。

⸻

第二个：

spec.provider_name != expected

表示：

provider_name 必须严格符合前面规定的命名规则。

比如：

source = local
name = query_balance
provider_name = query_balance

合法。

但：

source = local
name = query_balance
provider_name = balance

不合法。

因为：

expected = query_balance
actual   = balance

不一致。

⸻

18. 为什么不直接相信 provider_name？

因为 provider_name 属于一个外部可见的标识。

如果允许随便填写：

provider_name = "xxx"

那么：

ToolKey
    ↓
和
provider_name
    ↓
可能表达不同工具

整个系统的工具身份就容易混乱。

所以这里强制：

ToolKey
    ↓
计算 expected provider_name
    ↓
和实际 provider_name 比较

保证：

一个工具的内部身份和对外暴露名称之间存在确定关系。

⸻

19. 接下来检查 namespace

if (
    spec.key.source == "local"
    and spec.key.namespace is not None
) or (
    spec.key.source == "mcp"
    and not spec.key.namespace
):
    raise ValueError(...)

这里实际上是在规定：

local → 不允许 namespace
mcp   → 必须有 namespace

也就是：

local

source = local
namespace = None

合法。

source = local
namespace = ams

非法。

⸻

MCP

source = mcp
namespace = ams

合法。

source = mcp
namespace = None

非法。

因为 MCP 的名字需要：

mcp__namespace__name

没有 namespace 就无法形成规范名称。

⸻

20. 接下来检查重复

if spec.provider_name in names or spec.key in keys:
    raise ValueError(f"Duplicate tool: {spec.provider_name}")

这里实际上检查两个层面的唯一性。

第一层：provider_name 唯一

例如：

tool1.provider_name = "query_balance"
tool2.provider_name = "query_balance"

那么：

spec.provider_name in names

就是：

True

报错。

⸻

第二层：ToolKey 唯一

即使名字不同：

ToolKey("local", None, "query_balance")
ToolKey("local", None, "query_balance")

依然属于同一个工具身份。

所以：

spec.key in keys

也必须保证：

ToolKey 唯一

⸻

21. 这里其实有两种“唯一性”

这个设计挺严谨：

provider_name
    ↓
对外暴露名称唯一
spec.key
    ↓
内部工具身份唯一

两者都检查。

因为理论上：

身份唯一
≠
展示名称唯一

所以两个维度都约束。

⸻

22. 正则检查

if not re.fullmatch(
    r"[a-zA-Z0-9_-]{1,64}",
    spec.provider_name
):

这表示：

provider_name 必须满足：

只能包含：
a-z
A-Z
0-9
_
-

长度：

1 ~ 64

例如：

query_balance
query-balance
QueryBalance123

合法。

但：

query balance

不合法，因为有空格。

query.balance

不合法，因为有 .。

query/balance

不合法，因为有 /。

⸻

23. 为什么使用 fullmatch？

re.fullmatch(...)

意味着：

整个字符串都必须符合规则。

例如：

re.fullmatch(r"[a-z]+", "abc")

成功。

但是：

re.fullmatch(r"[a-z]+", "abc123")

失败。

这和：

re.search()

不一样。

search 只需要找到一部分匹配即可。

这里显然希望：

整个 provider_name 都符合规范。

所以用 fullmatch 很合适。

⸻

24. Schema 检查

接下来：

schema = thaw(spec.input_schema)

你这里应该有一个 thaw() 函数，用来把某种不可变结构恢复成普通可处理的 Python 结构。

例如可能是：

MappingProxyType
tuple

转成：

dict
list

最终得到：

schema

类似：

{
    "type": "object",
    "properties": {
        "account_id": {
            "type": "string"
        }
    },
    "required": ["account_id"]
}

⸻

25. 为什么要求 schema 根节点必须是 object？

if schema.get("type") != "object":
    raise ValueError(...)

意思是：

Tool 的输入参数必须是一个 JSON Object。

例如允许：

{
  "account_id": "123"
}

而不是：

"123"

或者：

["123", "456"]

这很符合 Tool Calling 的设计。

因为 Agent 调用工具一般是：

tool(arguments)

其中：

arguments
    ↓
JSON Object
    ↓
参数名 → 参数值

比如：

{
    "account_id": "123",
    "date": "2026-09-22"
}

⸻

26. Draft202012Validator.check_schema(schema) 是什么？

这个不是在检查：

某个具体参数是否符合 schema。

而是在检查：

这个 schema 自己是不是一个合法的 JSON Schema。

例如：

schema = {
    "type": "object",
    "properties": {
        "account_id": {
            "type": "string"
        }
    }
}

这是一个合法 schema。

但如果 schema 本身写得乱七八糟：

schema = {
    "type": "xxx"
}

就会被发现。

所以这里其实有两层检查：

第一层
schema.get("type") == "object"
        ↓
Tool 参数必须是 Object
第二层
Draft202012Validator.check_schema(schema)
        ↓
整个 JSON Schema 语法必须合法

⸻

27. 然后真正建立索引

names[spec.provider_name] = binding

例如：

binding1
provider_name = query_balance

就得到：

names = {
    "query_balance": binding1
}

继续：

binding2
provider_name = query_cost

得到：

names = {
    "query_balance": binding1,
    "query_cost": binding2
}

⸻

28. 同时记录 key

keys.add(spec.key)

这样下一个工具进来时：

spec.key in keys

就可以快速判断是否重复。

因为 set 查询通常是：

O(1)

平均情况下很快。

⸻

29. 到这里为什么还没有给 self.bindings 赋值？

因为：

@dataclass(frozen=True)

此时已经涉及冻结。

正常情况下：

self.bindings = bindings

可能会触发 frozen 的限制。

所以最后用了：

object.__setattr__

⸻

30. 这是整段代码最值得理解的一点

object.__setattr__(self, "bindings", bindings)

你可能会问：

都 frozen 了，为什么还能修改？

因为：

frozen=True

并不是让 Python 底层真的无法修改对象。

它主要是 dataclass 给你生成了一个禁止普通赋值的机制。

普通：

self.bindings = bindings

会被 frozen 拦截。

但是：

object.__setattr__(
    self,
    "bindings",
    bindings
)

直接调用 Python 对象底层的属性设置方法。

相当于绕过 dataclass 的 frozen 限制。

⸻

31. 为什么这里允许绕过 frozen？

因为这是对象初始化阶段。

逻辑是：

对象刚刚创建
      ↓
__post_init__
      ↓
内部计算
      ↓
最终设置正确的字段
      ↓
初始化完成
      ↓
以后不能修改

所以这里不是为了让外部绕过 frozen，而是为了：

在初始化过程中构造一个最终不可变对象。

这是 frozen dataclass 中非常常见的模式。

⸻

32. 接下来

object.__setattr__(
    self,
    "by_provider_name",
    MappingProxyType(names),
)

这里更有意思。

⸻

33. MappingProxyType 是什么？

假设：

names = {
    "tool1": binding1,
    "tool2": binding2,
}

普通 dict 是可以修改的：

names["tool3"] = binding3

如果直接：

self.by_provider_name = names

那么即使：

RequestToolSet(frozen=True)

也存在一个问题：

tool_set.by_provider_name["tool3"] = binding3

因为 frozen 只能防止：

tool_set.by_provider_name = xxx

不能防止：

tool_set.by_provider_name["xxx"] = ...

所以这里用了：

MappingProxyType(names)

它提供的是一个：

只读视图

于是：

tool_set.by_provider_name["tool1"]

可以。

但是：

tool_set.by_provider_name["tool3"] = binding3

不允许。

⸻

34. 所以这里形成了“双重不可变”

非常值得注意。

RequestToolSet
    frozen=True
        ↓
不能重新赋值字段
bindings
    tuple
        ↓
不能修改工具集合
by_provider_name
    MappingProxyType
        ↓
不能修改工具索引

最终大概是：

RequestToolSet
       │
       ├── bindings ──────────────→ tuple
       │                              ↓
       │                          ToolBinding
       │                          ToolBinding
       │                          ToolBinding
       │
       └── by_provider_name ─────→ read-only mapping
                                      ↓
                              name → ToolBinding

这就是一个非常典型的不可变配置/运行时上下文对象设计。

⸻

35. 最后看 create()

@classmethod
def create(cls, bindings: Iterable[ToolBinding]) -> RequestToolSet:
    return cls(tuple(bindings))

这个也很值得理解。

⸻

36. 为什么不直接让用户：

RequestToolSet([...])

而要：

RequestToolSet.create([...])

这里实际上是在提供一个工厂方法。

也就是说：

RequestToolSet.create(...)

是推荐的创建入口。

⸻

37. Iterable[ToolBinding] 是什么意思？

这里没有要求：

list[ToolBinding]

而是：

Iterable[ToolBinding]

意思是：

只要你能被 for 遍历就可以。

例如：

[
    tool1,
    tool2,
]

可以。

(
    tool1,
    tool2,
)

也可以。

甚至：

(tool for tool in tools)

也可以。

所以 API 更灵活。

⸻

38. 为什么 create() 里面又 tuple() 一次？

return cls(tuple(bindings))

因为它希望在进入 dataclass 构造函数之前，就把：

Iterable

规范化成：

tuple

比如：

generator

传进来：

RequestToolSet.create(
    tool for tool in tools
)

变成：

tuple(generator)

之后就是一个确定的快照。

⸻

39. 那为什么 __post_init__ 里还要：

bindings = tuple(self.bindings)

看起来重复了：

create()
    ↓
tuple(bindings)
    ↓
cls(...)
    ↓
__post_init__()
    ↓
tuple(self.bindings)

确实存在一定程度的重复。

但它有一个设计上的意义：

__post_init__ 自己也保证不管调用者通过什么方式构造对象，最终内部都是 tuple。

因为理论上用户可以绕过：

create()

直接：

RequestToolSet(
    bindings=some_iterable
)

那么 __post_init__ 仍然会规范化。

所以：

create()

负责对外提供友好的创建入口。

而：

__post_init__()

负责最终的不变量保证。

⸻

40. 整个生命周期串起来

假设：

tool1
tool2
tool3

执行：

tool_set = RequestToolSet.create([
    tool1,
    tool2,
    tool3,
])

完整过程是：

RequestToolSet.create(...)
          │
          ↓
tuple(bindings)
          │
          ↓
(
  tool1,
  tool2,
  tool3
)
          │
          ↓
cls(...)
          │
          ↓
dataclass __init__
          │
          ↓
bindings 被赋值
          │
          ↓
__post_init__()
          │
          ├── tuple(self.bindings)
          │
          ├── 遍历每个 ToolBinding
          │
          ├── 检查 source
          │
          ├── 计算 expected provider_name
          │
          ├── 检查 provider_name
          │
          ├── 检查 namespace
          │
          ├── 检查重复
          │
          ├── 检查名称格式
          │
          ├── 检查 schema root
          │
          ├── 检查 JSON Schema
          │
          ├── 建立 names
          │
          └── 建立 keys
          │
          ↓
object.__setattr__(...)
          │
          ├── bindings = tuple
          │
          └── by_provider_name = MappingProxyType
          │
          ↓
初始化完成
          │
          ↓
RequestToolSet

⸻

41. 最终得到的对象是什么样？

假设：

tool1.provider_name = "query_balance"
tool2.provider_name = "query_cost"
tool3.provider_name = "mcp__ams__create_ad"

最终：

tool_set.bindings

类似：

(
    tool1,
    tool2,
    tool3,
)

而：

tool_set.by_provider_name

类似：

{
    "query_balance": tool1,
    "query_cost": tool2,
    "mcp__ams__create_ad": tool3,
}

但是后者实际上是只读的：

MappingProxyType(...)

⸻

42. 为什么这个设计很适合 Agent Tool 系统？

因为 Tool 是一个非常适合做成不可变声明对象的东西。

你的 Agent 执行过程中可能有：

Request
  ↓
RequestToolSet
  ↓
Planner
  ↓
选择 Tool
  ↓
ToolBinding
  ↓
execute()

如果 ToolSet 可以在执行过程中随意变化：

Planner 看到 10 个工具
        ↓
执行过程中工具被修改
        ↓
实际执行环境只有 9 个

就会产生很多难排查的问题。

现在采用：

RequestToolSet
= immutable snapshot

可以理解成：

一次 Request 开始时，把这一轮 Agent Run 所允许使用的工具集合拍一个快照。

之后：

Planner
Agent
Tool Selector
Permission
Executor

都基于同一个 ToolSet。

⸻

43. 这段代码实际上在维护几个“不变量”

这是理解这类企业级代码非常重要的角度。

所谓 invariant，就是：

对象一旦成功创建，就永远应该满足的条件。

这个类大概维护了：

不变量 1：bindings 是不可变集合

bindings → tuple

⸻

不变量 2：每个工具的 source 合法

source ∈ {local, mcp}

⸻

不变量 3：provider_name 和 ToolKey 一致

local
    name
mcp
    mcp__namespace__name

⸻

不变量 4：namespace 使用正确

local → namespace = None
mcp → namespace != None

⸻

不变量 5：工具不能重复

provider_name 唯一
ToolKey 唯一

⸻

不变量 6：provider_name 格式合法

[a-zA-Z0-9_-]{1,64}

⸻

不变量 7：Tool 输入 Schema 合法

root type = object
+
符合 JSON Schema 2020-12

⸻

不变量 8：索引和 bindings 一致

bindings
    ↕
by_provider_name

⸻

44. 你可以把这段代码浓缩理解成一句话

RequestToolSet 是一个不可变的 Tool Registry：初始化时把一批 ToolBinding 做标准化和完整校验，并建立 provider_name → ToolBinding 的只读索引，确保后续 Agent 执行期间工具集合稳定且内部一致。

其中最核心的技术点其实就是这几个：

@dataclass
    ↓
自动生成初始化代码
frozen=True
    ↓
对象字段不可重新赋值
slots=True
    ↓
限制动态属性 + 节省对象开销
field(init=False)
    ↓
by_provider_name 不由调用者传入，而是内部计算
__post_init__
    ↓
初始化完成后做校验和派生字段构建
object.__setattr__
    ↓
frozen 对象初始化阶段绕过限制写入字段
tuple
    ↓
工具集合不可变
MappingProxyType
    ↓
工具索引不可变
create()
    ↓
提供统一、友好的构造入口

如果你正在看这套 Agent 代码，我建议尤其记住一个模式：

@dataclass(frozen=True)
class Xxx:
    raw_data: ...
    derived_data: ... = field(init=False)
    def __post_init__(self):
        # 校验 raw_data
        # 根据 raw_data 计算 derived_data
        object.__setattr__(self, "derived_data", ...)

这是**“不可变对象 + 初始化时派生数据”**的经典 Python 写法。
'''