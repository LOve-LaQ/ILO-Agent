import clsx from 'clsx';

import { useToastStore } from './toastStore';
import styles from './Toast.module.css';

/** 全局 Toast 宿主，挂载在 App 根部 */
export function Toast() {
  const message = useToastStore((state) => state.message);

  return (
    <div className={clsx(styles.toast, message && styles.show)} role="status" aria-live="polite">
      {message}
    </div>
  );
}
