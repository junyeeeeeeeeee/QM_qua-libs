# Academia Sinica - QPU Calibration Library

![Banner](GITHUB-BANNER.jpg)

## Overview
This repository provides a comprehensive library for calibrating superconducting transmon qubits using the Quantum Orchestration Platform (QOP), QUAM, and QUAlibrate. This includes both flux-tunable and fixed-frequency Transmons. It includes configurable experiment nodes, analysis routines, and tools for managing the quantum system state (QUAM).

This library is built upon QUAlibrate, an advanced, open-source software framework designed specifically for the automated calibration of Quantum Processing Units (QPUs). QUAlibrate provides tools to create, manage, and execute calibration routines efficiently. The configurable experiment nodes, analysis routines, and state management tools included here are designed to integrate seamlessly with the QUAlibrate ecosystem. See the QUAlibrate Documentation for more information.

## Getting Started
The main library resides in the `Quantum-Control-Applications-QuAM/Superconducting/` directory. 

Please visit its [README](Quantum-Control-Applications-QuAM/Superconducting/README.md) to get started.

## JY agentic measurement / JY 代理量測

After the one-time JY setup, open this repository root in Codex, Claude Code, or
Cursor and enter `進入 JY 量測模式` or `進入 JY 自動量測模式`. You do not need
to open the nested `JY_agent` directory, and the outer repository folder may be
renamed. See the bilingual
[JY_agent README](Quantum-Control-Applications-QuAM/Superconducting/JY_agent/README.md)
for installation, approval, Dashboard, and safety details.
The bilingual command reference is
[Command.md](Quantum-Control-Applications-QuAM/Superconducting/JY_agent/Command.md).

完成一次性 JY 安裝後，直接用 Codex、Claude Code 或 Cursor 開啟本 repository
最上層，輸入 `進入 JY 量測模式` 或 `進入 JY 自動量測模式` 即可；不必再把
`JY_agent` 子資料夾另外開成 workspace，而且最外層 repository 資料夾可以改名。
