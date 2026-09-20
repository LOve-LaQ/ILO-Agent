import { useQuery } from '@tanstack/react-query';

import { fetchCaptchaConfig } from '../api';

/**
 * 拉取人机校验配置。
 *
 * 【为什么 staleTime 设成 Infinity】这是启动期读取一次就固定的环境配置（provider/vid），
 * 一个会话内不会变；设置成永不过期避免每次进登录/注册页都多打一次请求。
 * 组件据 `enabled` 决定是否渲染验证控件，据 `vid` 初始化厂商 SDK。
 */
export function useCaptchaConfig() {
  return useQuery({
    queryKey: ['auth', 'captcha-config'],
    queryFn: fetchCaptchaConfig,
    staleTime: Infinity,
    retry: 1,
  });
}
