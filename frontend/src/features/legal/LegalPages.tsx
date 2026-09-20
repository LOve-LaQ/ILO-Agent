import { Link } from 'react-router-dom';

import styles from './LegalPage.module.css';

/**
 * 隐私政策与用户协议静态页。
 *
 * 【占位提示】正文为通用模板，**主体名称、联系方式、生效日期需按实际运营方替换**。
 * 这些内容属于「法律事实」，不能由代码推断，必须由运营方确认。
 */
const DOCUMENT_VERSION = '2026-01';

function LegalShell({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <div className={styles.page}>
      <article className={styles.doc}>
        <div className={styles.brand}>
          <span className={styles.brandMark}>I</span>
          <span>ILO 技术情报官</span>
        </div>
        <h1 className={styles.title}>{title}</h1>
        <p className={styles.updated}>版本 {DOCUMENT_VERSION}</p>
        {children}
        <div className={styles.note}>
          本页为通用模板，运营主体名称、联系方式与生效日期需以实际备案信息为准。
        </div>
        <p className={styles.foot}>
          <Link to="/register">← 返回注册</Link>
        </p>
      </article>
    </div>
  );
}

export function PrivacyPage() {
  return (
    <LegalShell title="隐私政策">
      <p>
        我们仅收集为你提供「技术情报发现 - 卡片学习 - 学习记录」所必需的信息，并遵循
        「最小必要」原则。本政策说明我们收集什么、用来做什么、保存多久以及你拥有的权利。
      </p>

      <h2>一、我们收集的信息</h2>
      <table>
        <thead>
          <tr>
            <th>信息类别</th>
            <th>具体内容</th>
            <th>用途</th>
          </tr>
        </thead>
        <tbody>
          <tr>
            <td>账号信息</td>
            <td>邮箱、用户名、密码（仅保存不可逆的 bcrypt 哈希）</td>
            <td>注册、登录、找回密码</td>
          </tr>
          <tr>
            <td>学习内容</td>
            <td>卡片收藏、学习会话与对话记录</td>
            <td>为你保留学习进度与历史</td>
          </tr>
          <tr>
            <td>行为记录</td>
            <td>登录、收藏、导出等操作的时间与类型；IP 的 HMAC 指纹；浏览器标识（User-Agent）</td>
            <td>安全审计与异常排查</td>
          </tr>
          <tr>
            <td>登录设备</td>
            <td>会话标识、设备摘要、IP 哈希前缀、创建与最近活跃时间</td>
            <td>设备管理与「改密后立即下线」</td>
          </tr>
        </tbody>
      </table>
      <p>
        <strong>我们不保存明文 IP</strong>：所有 IP 以独立密钥的 HMAC-SHA256 指纹形式存储，
        不可反查。浏览器标识会被截断，仅保留有限长度。
      </p>

      <h2>二、信息的使用与共享</h2>
      <ul>
        <li>我们不会将你的个人信息出售给第三方。</li>
        <li>为完成人机校验，注册 / 找回密码等环节会把你的一次性验证 token 提交给验证码服务商核验。</li>
        <li>为投递找回密码邮件，我们会把收件邮箱与邮件内容交给所配置的邮件服务商。</li>
      </ul>

      <h2>三、保留期限</h2>
      <ul>
        <li>行为记录：180 天；</li>
        <li>已撤销 / 过期的登录会话：90 天；</li>
        <li>邮件发送记录：30 天；</li>
        <li>已使用的密码重置令牌：7 天。</li>
      </ul>

      <h2>四、你的权利</h2>
      <ul>
        <li>
          <strong>访问与导出：</strong>在「账号安全」页可一键导出你的全部数据（JSON）。
        </li>
        <li>
          <strong>更正与删除：</strong>可修改密码，或申请注销账号；注销经冷静期后，账号信息将被匿名化、
          个人内容将被删除。
        </li>
        <li>
          <strong>撤销同意：</strong>你可随时申请注销以停止使用服务。
        </li>
      </ul>

      <h2>五、安全措施</h2>
      <ul>
        <li>密码使用 bcrypt 加盐哈希存储，登录凭证短期有效并支持服务端撤销；</li>
        <li>登录 / 注册 / 找回密码设有频率限制与人机校验，并记录安全事件。</li>
      </ul>
    </LegalShell>
  );
}

export function TermsPage() {
  return (
    <LegalShell title="用户协议">
      <p>
        在使用 ILO 技术情报官（下称「本服务」）前，请仔细阅读本协议。注册或使用本服务，
        即表示你已阅读并同意本协议与《隐私政策》。
      </p>

      <h2>一、账号</h2>
      <ul>
        <li>你应提供真实、有效的邮箱用于注册，并妥善保管账号与密码。</li>
        <li>账号行为由你负责；如发现账号被盗用，请立即修改密码并下线其他设备。</li>
        <li>用户名不得冒充官方或他人，不得含有违法、侵权或误导性内容。</li>
      </ul>

      <h2>二、使用规范</h2>
      <ul>
        <li>不得利用本服务从事违法违规活动，不得尝试绕过安全限制、批量注册或攻击本服务。</li>
        <li>不得对本服务内容进行未经许可的抓取、复制或商业利用。</li>
      </ul>

      <h2>三、内容与知识产权</h2>
      <ul>
        <li>本服务所汇集的第三方技术内容，其权利归原作者或原平台所有，本服务仅作学习用途的聚合与摘要。</li>
        <li>AI 生成的讲解与摘要仅供参考，不构成任何专业建议。</li>
      </ul>

      <h2>四、服务变更与终止</h2>
      <ul>
        <li>你可以随时在「账号安全 → 注销账号」申请注销，账号将在冷静期后被永久删除。</li>
        <li>我们可能因维护、升级或不可抗力调整或中断服务。</li>
      </ul>

      <h2>五、免责声明</h2>
      <p>在法律允许的范围内，本服务按「现状」提供，不对内容的准确性、完整性作出保证。</p>
    </LegalShell>
  );
}
