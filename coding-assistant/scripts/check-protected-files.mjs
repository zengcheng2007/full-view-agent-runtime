#!/usr/bin/env node
// check-protected-files — PreToolUse hook
// 从 CLAUDE.md 和 coding-assistant/tools/00_项目配置/项目信息.md 读取文件边界，
// 拦截 Edit/Write 对只读/禁止修改区域的写入。
// Usage: Configure in .claude/settings.json PreToolUse hook.

import { readFileSync, existsSync } from 'fs';
import { join, resolve, relative } from 'path';

const chunks = [];
process.stdin.on('data', c => chunks.push(c));
process.stdin.on('end', () => {
  try {
    const data = JSON.parse(Buffer.concat(chunks).toString().replace(/^\uFEFF/, ''));
    const toolName = data?.tool_name || data?.toolName || '';
    const filePath = data?.tool_input?.file_path || data?.toolInput?.file_path || '';

    if (!['Edit', 'Write', 'edit', 'write'].includes(toolName)) {
      console.log(JSON.stringify({ continue: true, suppressOutput: true }));
      return;
    }
    if (!filePath) {
      console.log(JSON.stringify({ continue: true, suppressOutput: true }));
      return;
    }

    // Always allow .claude/ and CLAUDE.md
    const clean = filePath.replace(/\\/g, '/');
    if (clean.startsWith('.claude/') || clean === 'CLAUDE.md') {
      console.log(JSON.stringify({ continue: true, suppressOutput: true }));
      return;
    }

    const result = checkBoundary(clean, toolName);
    console.log(JSON.stringify(result));
  } catch (error) {
    console.log(
      JSON.stringify({
        continue: false,
        reason: 'file-boundary-check-failed',
        message: `[FILE BOUNDARY CHECK FAILED] ${error instanceof Error ? error.message : String(error)}`,
      }),
    );
  }
});

function checkBoundary(targetFile, toolName) {
  const cwd = process.cwd();
  const relTarget = targetFile.startsWith(cwd)
    ? relative(cwd, targetFile).replace(/\\/g, '/')
    : targetFile;

  // Collect all allowed (readWrite) and blocked (readOnly + forbidden) paths
  const allowed = [];
  const blocked = [];

  // Source 1: CLAUDE.md (Markdown list format under "可读写" / "可只读" / "禁止修改")
  const claudeMd = join(cwd, 'CLAUDE.md');
  if (existsSync(claudeMd)) {
    const content = readFileSync(claudeMd, 'utf-8');
    parseClaudeMdBoundary(content, allowed, blocked);
  }

  // Source 2: coding-assistant/tools/00_项目配置/项目信息.md (区域/读写权限 format)
  const projectInfo = join(cwd, 'coding-assistant/tools/00_项目配置/项目信息.md');
  if (existsSync(projectInfo)) {
    const content = readFileSync(projectInfo, 'utf-8');
    parseProjectInfoBoundary(content, allowed, blocked);
  }

  if (allowed.length === 0 && blocked.length === 0) {
    return { continue: true, suppressOutput: true };
  }

  // Check blocked first (explicit deny > implicit allow)
  for (const pattern of blocked) {
    if (matchPath(relTarget, pattern)) {
      return {
        continue: false,
        reason: 'file-boundary-violation',
        message: [
          `[FILE BOUNDARY VIOLATION] Cannot ${toolName || 'write'}: ${relTarget}`,
          '',
          `This file matches blocked pattern: ${pattern}`,
          'Stop and ask the user how to proceed. Do not bypass this check.',
        ].join('\n'),
      };
    }
  }

  // Check allowed — if no allowed match, block
  if (allowed.length > 0) {
    const isAllowed = allowed.some(p => matchPath(relTarget, p));
    if (!isAllowed) {
      return {
        continue: false,
        reason: 'file-boundary-violation',
        message: [
          `[FILE BOUNDARY VIOLATION] Cannot ${toolName || 'write'}: ${relTarget}`,
          '',
          'This file is not in any "可读写" or "读写" boundary. Allowed paths:',
          ...allowed.map(p => `  - ${p}`),
          '',
          'Stop and ask the user how to proceed. Do not bypass this check.',
        ].join('\n'),
      };
    }
  }

  return { continue: true, suppressOutput: true };
}

// Parse CLAUDE.md format: ### 可读写 / ### 可只读 / ### 禁止修改
function parseClaudeMdBoundary(content, allowed, blocked) {
  const rwSec = extractSection(content, /###?\s*可读写/i, /###?\s*可只读|###?\s*禁止修改|$/i);
  const roSec = extractSection(content, /###?\s*可只读/i, /###?\s*禁止修改|###?\s*可读写|$/i);
  const fbSec = extractSection(content, /###?\s*禁止修改/i, /###?\s*可读写|###?\s*可只读|$/i);

  addListPaths(rwSec, allowed);
  addListPaths(roSec, blocked);
  addListPaths(fbSec, blocked);
}

function extractSection(content, startRe, endRe) {
  const start = content.search(startRe);
  if (start === -1) return '';
  const afterStart = content.indexOf('\n', start);
  const end = content.slice(afterStart).search(endRe);
  return end === -1 ? content.slice(afterStart) : content.slice(afterStart, afterStart + end);
}

function addListPaths(section, target) {
  const lines = (section.match(/^[\s]*[-*]\s*[`"]?([^`"\n]+)[`"]?/gm) || [])
    .map(s => s.replace(/^[\s]*[-*]\s*[`"]?/, '').replace(/[`"]?$/, '').trim())
    .filter(Boolean);
  lines.forEach(p => target.push(p));
}

// Parse 项目信息.md format: 区域: xxx / 读写权限: xxx / 说明: xxx
function parseProjectInfoBoundary(content, allowed, blocked) {
  const blocks = content.split(/─{3,}/);
  for (const block of blocks) {
    const areaMatch = block.match(/区域:\s*(.+)/);
    if (!areaMatch) continue;
    const area = areaMatch[1].trim();
    const permMatch = block.match(/读写权限:\s*(.+)/);
    if (!permMatch) continue;
    const perm = permMatch[1].trim();

    if (perm.includes('读写') || perm.includes('可修改')) {
      allowed.push(area);
    } else if (perm.includes('只读') || perm.includes('禁止')) {
      blocked.push(area);
    }
  }

  // Also parse the 前端/后端 — 可读写/可只读/禁止修改 Markdown list format
  const rwMatch = content.match(/###?\s*(?:前端|后端)?\s*[—\-]?\s*可读写[\s\S]*?(?=###|$)/gi) || [];
  const roMatch = content.match(/###?\s*(?:前端|后端)?\s*[—\-]?\s*可只读[\s\S]*?(?=###|$)/gi) || [];
  const fbMatch = content.match(/###?\s*(?:前端|后端)?\s*[—\-]?\s*禁止修改[\s\S]*?(?=###|$)/gi) || [];

  rwMatch.forEach(s => addListPaths(s, allowed));
  roMatch.forEach(s => addListPaths(s, blocked));
  fbMatch.forEach(s => addListPaths(s, blocked));
}

// Match a file path against a boundary pattern (glob-style)
function matchPath(filePath, pattern) {
  // Normalize
  let p = pattern.replace(/\\/g, '/').replace(/\/$/, '');

  // Handle ** glob
  if (p.includes('**')) {
    const re = new RegExp('^' + p.replace(/\*\*/g, '___GLOB___').replace(/\*/g, '[^/]*').replace(/___GLOB___/g, '.*') + '$');
    return re.test(filePath);
  }

  // Handle * glob
  if (p.includes('*')) {
    const re = new RegExp('^' + p.replace(/\*/g, '[^/]*') + '$');
    return re.test(filePath);
  }

  // Exact prefix or directory match
  if (filePath.startsWith(p + '/') || filePath === p) return true;

  // Parenthesized suffix like (全局类型定义)
  p = p.replace(/\([^)]*\)/g, '').trim();
  if (p && (filePath.startsWith(p + '/') || filePath === p)) return true;

  return false;
}
