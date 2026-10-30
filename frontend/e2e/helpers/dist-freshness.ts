import { readdirSync, statSync } from "node:fs";
import { join } from "node:path";

// preview 服务的是 dist/：直接跑 `npx playwright test` 不会先 build，改完 .vue 会测到上一个产物（TD-254）。
// 比较 dist/index.html 与构建输入的最新 mtime，输入更新（含新增/删除文件）就中止，而不是让用例绿得可疑。
const SHARED_INPUTS = [
  "packages/api-client/package.json",
  "packages/api-client/src",
  "packages/config/tsconfig.base.json",
  "packages/ui/package.json",
  "packages/ui/src",
];
const ROOT_INPUTS = ["package.json", "pnpm-lock.yaml"];

// 单测与生成的 d.ts 不进产物；它们变新不该逼着重建，build 自己写 d.ts 更不能把前检顶红。
// 代价：新增/删除这类文件会更新目录 mtime，可能多提示一次 build——偏向安全的方向，接受。
const IGNORED_FILE = /\.(test|spec)\.ts$|\.d\.ts$/;

function newestMtime(path: string): number {
  let stat;
  try {
    stat = statSync(path);
  } catch {
    throw new Error(`[e2e] 缺少构建输入 ${path}：先运行 pnpm install（TD-254）`);
  }
  if (!stat.isDirectory()) {
    return stat.mtimeMs;
  }
  // 目录自身 mtime 计入：新增/删除文件会更新它，只比现存文件的 mtime 会漏掉「删了源码没重建」。
  return readdirSync(path).reduce(
    (newest, entry) =>
      IGNORED_FILE.test(entry) ? newest : Math.max(newest, newestMtime(join(path, entry))),
    stat.mtimeMs,
  );
}

export function assertDistFresh(app: "admin" | "campus", frontendRoot: string): void {
  const appRoot = join(frontendRoot, "apps", app);
  const entry = join(appRoot, "dist", "index.html");
  let distMtime: number;
  try {
    distMtime = statSync(entry).mtimeMs;
  } catch {
    throw new Error(`[e2e] 找不到 ${entry}：先运行 pnpm build（TD-254）`);
  }
  const inputs = [
    join(appRoot, "src"),
    join(appRoot, "index.html"),
    join(appRoot, "tsconfig.json"),
    join(appRoot, "vite.config.ts"),
    join(appRoot, "package.json"),
    ...readdirSync(appRoot)
      .filter((name) => name.startsWith(".env"))
      .map((name) => join(appRoot, name)),
    ...SHARED_INPUTS.map((path) => join(frontendRoot, path)),
    ...ROOT_INPUTS.map((path) => join(frontendRoot, path)),
  ];
  const newestInput = Math.max(...inputs.map(newestMtime));
  if (newestInput > distMtime) {
    throw new Error(`[e2e] ${app} 的 dist 比构建输入旧：先运行 pnpm build（TD-254）`);
  }
}
