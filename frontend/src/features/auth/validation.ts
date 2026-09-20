/**
 * 注册 / 改密的前端校验镜像。
 *
 * 【为什么要镜像】后端是唯一权威（backend/src/core/password_policy.py、
 * backend/src/core/username_policy.py），这里只是把同一套规则提前到输入时反馈，
 * 避免「填完整张表才被打回」。**规则必须与后端逐条对齐**，后端改动时这里要同步，
 * 否则会出现「前端放行、后端 422」的割裂体验。
 *
 * 三条一致性约定：
 * - 长度按**字节**算（bcrypt 的 72 字节上限是字节口径，不是字符口径）；
 * - 弱密码黑名单、连续序列、重复字符的判定窗口与后端相同；
 * - 校验函数返回**拒绝原因字符串**而非布尔值，好让错误提示说清「为什么不行」。
 */

export const USERNAME_MIN = 2;
export const USERNAME_MAX = 50;
export const PASSWORD_MIN_BYTES = 8;
export const PASSWORD_MAX_BYTES = 72;

const IDENTITY_MIN_FRAGMENT = 4;
const SEQUENCE_RUN = 4;
const REPEAT_RUN = 4;

const USERNAME_ALLOWED = /^[A-Za-z0-9_-]+$/;
const USERNAME_REPEATED_SEPARATOR = /[_-]{2}/;

/** 键盘行走 / 数字 / 字母序列（与后端 _SEQUENCES 一致） */
const SEQUENCES = [
  '0123456789',
  'abcdefghijklmnopqrstuvwxyz',
  'qwertyuiop',
  'asdfghjkl',
  'zxcvbnm',
  '1qaz2wsx',
  'qazwsxedc',
];

/** 常见弱密码黑名单（与后端 _COMMON_PASSWORDS 一致，命中不区分大小写） */
const COMMON_PASSWORDS = new Set([
  '123456', 'password', '12345678', 'qwerty', '123456789', '12345', '1234',
  '111111', '1234567', '123123', 'abc123', '666666', '123321', '654321',
  '1234567890', '000000', '7777777', '121212', '112233', '555555', '131313',
  '777777', '888888', '222222', '987654321', '1234qwer', '11111111',
  'qwertyuiop', 'zxcvbnm', 'asdfgh', 'qazwsx', '1qaz2wsx', 'qwer1234',
  '1q2w3e4r', '1qazxsw2', 'q1w2e3r4t5', 'qaz123', 'qweqwe', 'asdfasdf',
  '0987654321', '987654', '789456', '123654', '456789', '11223344',
  'password1', 'password123', 'passw0rd', 'p@ssw0rd', 'letmein', 'welcome',
  'welcome1', 'admin', 'admin123', 'administrator', 'root', 'root123',
  'test', 'test123', 'guest', 'master', 'master123', 'changeme', 'default',
  'secret', 'summer', 'winter', 'spring', 'autumn', 'monkey', 'dragon',
  'baseball', 'football', 'soccer', 'hockey', 'batman', 'superman',
  'starwars', 'matrix', 'phoenix', 'falcon', 'tigger', 'sunshine',
  'iloveyou', 'iloveu', 'loveyou', 'love', 'angel', 'shadow', 'ninja',
  'samsung', 'samsung123', 'huawei', 'xiaomi123', 'apple123', 'google123',
  'computer', 'internet', 'access', 'gateway', 'network', 'system',
  'server', 'database', 'oracle', 'mysql', 'postgres', 'sqlserver',
  'qwerty123', 'abc123456', '123abc', 'a123456', 'a123456789', 'aa123456',
  '123456a', '123456qq', 'qq123456', '123456789a', '12345678910',
  '5201314', 'woaini', 'woaini1314', 'woaini520', '1314520', '520520',
  'zxcvbnm123', 'qqqqqq', 'aaaaaa', 'aaaaaaa', 'aaaaaaaa', 'zzzzzz',
  'xxxxxx', 'abcdef', 'abcabc', 'abcd1234', 'abcd123456', 'qwertyui',
  'asdfghjkl', 'zxcvb', 'poiuyt', 'lkjhgf', 'mnbvcxz', 'qwerty12345',
  'hello', 'hello123', 'hi123456', 'freedom', 'whatever', 'trustno1',
  'mustang', 'camaro', 'corvette', 'ferrari', 'mercedes', 'porsche',
  'yamaha', 'harley', 'toyota', 'honda', 'nissan', 'guitar', 'piano',
  'cookie', 'orange', 'banana', 'pepper', 'ginger', 'purple', 'yellow',
  'silver', 'golden', 'diamond', 'magic', 'flying', 'running', 'walker',
  'hunter', 'killer', 'sniper', 'ranger', 'soldier', 'pilot', 'captain',
  'doctor', 'nurse', 'teacher', 'student', 'school', 'college', 'university',
  '010203', '102030', '456123', '654123', '741852', '852963',
  '963852', '159357', '753951', '159753', '951753', '74108520',
]);

/** 保留用户名（与后端 RESERVED_USERNAMES 一致，命中不区分大小写） */
const RESERVED_USERNAMES = new Set([
  'admin', 'administrator', 'root', 'system', 'sysadmin', 'superuser',
  'support', 'service', 'help', 'helpdesk', 'official', 'staff', 'team',
  'owner', 'moderator', 'operator', 'security', 'abuse', 'postmaster',
  'webmaster', 'hostmaster', 'noreply', 'no-reply', 'mail', 'email',
  'api', 'www', 'web', 'app', 'static', 'assets', 'public', 'internal',
  'me', 'user', 'users', 'account', 'accounts', 'profile', 'settings',
  'login', 'logout', 'register', 'signup', 'signin', 'auth', 'oauth',
  'library', 'discover', 'dashboard', 'console', 'portal', 'home',
  'ilo', 'iloagent', 'ilo-agent', 'agent', 'bot', 'robot', 'crawler',
  'test', 'testing', 'demo', 'example', 'sample', 'guest', 'anonymous',
  'null', 'undefined', 'none', 'nan', 'true', 'false',
  'billing', 'payment', 'invoice', 'order', 'subscribe', 'newsletter',
  'legal', 'privacy', 'terms', 'policy', 'about', 'contact', 'blog',
  'news', 'docs', 'documentation', 'status', 'health', 'metrics',
]);

/** 按 UTF-8 字节计算长度 */
export function byteLength(value: string): number {
  return new TextEncoder().encode(value).length;
}

function hasLongRepeat(value: string): boolean {
  let run = 1;
  for (let index = 1; index < value.length; index += 1) {
    if (value[index] === value[index - 1]) {
      run += 1;
      if (run >= REPEAT_RUN) return true;
    } else {
      run = 1;
    }
  }
  return false;
}

function hasSequence(value: string): boolean {
  const lowered = value.toLowerCase();
  for (const sequence of SEQUENCES) {
    for (let start = 0; start <= sequence.length - SEQUENCE_RUN; start += 1) {
      const fragment = sequence.slice(start, start + SEQUENCE_RUN);
      const reversed = fragment.split('').reverse().join('');
      if (lowered.includes(fragment) || lowered.includes(reversed)) {
        return true;
      }
    }
  }
  return false;
}

function identityFragments(username?: string, email?: string): string[] {
  const fragments: string[] = [];
  for (const raw of [username, (email ?? '').split('@')[0]]) {
    const candidate = (raw ?? '').trim().toLowerCase();
    if (candidate.length >= IDENTITY_MIN_FRAGMENT) {
      fragments.push(candidate);
    }
  }
  return fragments;
}

/** 校验密码；通过返回 null，否则返回面向用户的拒绝原因（与后端 check_password 对齐） */
export function checkPassword(
  password: string,
  options: { username?: string; email?: string } = {},
): string | null {
  if (byteLength(password) < PASSWORD_MIN_BYTES) {
    return `密码至少需要 ${PASSWORD_MIN_BYTES} 个字符。`;
  }
  if (byteLength(password) > PASSWORD_MAX_BYTES) {
    return `密码过长（最多 ${PASSWORD_MAX_BYTES} 字节）。`;
  }

  const lowered = password.toLowerCase();
  if (COMMON_PASSWORDS.has(lowered)) {
    return '这个密码太常见了，容易被猜到，请换一个。';
  }
  if (hasLongRepeat(password)) {
    return `密码不能包含连续 ${REPEAT_RUN} 个以上相同字符。`;
  }
  if (hasSequence(password)) {
    return `密码不能包含连续 ${SEQUENCE_RUN} 位以上的顺序字符（如 1234、abcd）。`;
  }
  for (const fragment of identityFragments(options.username, options.email)) {
    if (lowered.includes(fragment)) {
      return '密码不能包含用户名或邮箱中的连续片段。';
    }
  }
  return null;
}

/** 校验用户名；通过返回 null，否则返回面向用户的拒绝原因（与后端 check_username 对齐） */
export function checkUsername(value: string): string | null {
  const candidate = (value ?? '').trim();
  if (candidate.length < USERNAME_MIN) {
    return `用户名至少需要 ${USERNAME_MIN} 个字符。`;
  }
  if (candidate.length > USERNAME_MAX) {
    return `用户名最多 ${USERNAME_MAX} 个字符。`;
  }
  if (!USERNAME_ALLOWED.test(candidate)) {
    return '用户名只能包含字母、数字、下划线和连字符。';
  }
  if (candidate.startsWith('_') || candidate.startsWith('-') || candidate.endsWith('_') || candidate.endsWith('-')) {
    return '用户名不能以下划线或连字符开头或结尾。';
  }
  if (USERNAME_REPEATED_SEPARATOR.test(candidate)) {
    return '用户名不能包含连续的下划线或连字符。';
  }
  if (RESERVED_USERNAMES.has(candidate.toLowerCase())) {
    return '该用户名为系统保留，请换一个。';
  }
  return null;
}

export type PasswordStrength = {
  /** 0–4：0 表示未通过策略（红色），4 表示很强（绿色） */
  score: 0 | 1 | 2 | 3 | 4;
  label: string;
};

/**
 * 粗略强度评分（仅用于强度条视觉反馈，不作为放行依据；放行一律看 checkPassword）。
 *
 * 评分只做加法：长度、字符种类各给分。它不需要很准 —— 目的是让用户看到「再加长一点」
 * 的正反馈，而不是把弱密码判成强密码。
 */
export function passwordStrength(password: string): PasswordStrength {
  if (!password) return { score: 0, label: '' };
  if (checkPassword(password)) return { score: 0, label: '不达标' };

  const length = byteLength(password);
  let points = 1;
  if (length >= 12) points += 1;
  if (length >= 16) points += 1;

  const classes = [/[a-z]/, /[A-Z]/, /[0-9]/, /[^A-Za-z0-9]/].filter((pattern) =>
    pattern.test(password),
  ).length;
  if (classes >= 2) points += 1;
  if (classes >= 3) points += 1;

  const score = Math.min(4, points) as 1 | 2 | 3 | 4;
  const labels: Record<number, string> = { 1: '一般', 2: '中等', 3: '较强', 4: '很强' };
  return { score, label: labels[score] };
}
