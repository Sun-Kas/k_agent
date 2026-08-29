import React from "react";
import { Text } from "ink";
import { TERMINAL_DESIGN } from "../design.js";
import { ScreenFrame } from "../panels/ScreenFrame.js";

export function DoctorScreen({ lines }: { lines: string[] }): React.ReactElement {
  return (
    <ScreenFrame title="DOCTOR" count={lines.length ? `${lines.length} 项` : ""}>
      {lines.length
        ? lines.map((line, index) => <Text key={`${index}-${line}`}>{line}</Text>)
        : <Text color={TERMINAL_DESIGN.colors.muted}>正在检查 Access Layer 与终端能力…</Text>}
    </ScreenFrame>
  );
}
