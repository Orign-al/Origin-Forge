"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useEffect, useState } from "react";
import { useForm } from "react-hook-form";
import { z } from "zod";

import { ACCESS_MODE_LABEL } from "@h100-portal/config";
import { Button, Card, Input } from "@h100-portal/ui";
import { getCsrf, login } from "../../lib/api";

const schema = z.object({
  username: z.string().min(1, "请输入登录名"),
  password: z.string().min(1, "请输入密码"),
});
type FormValues = z.infer<typeof schema>;

export default function LoginPage() {
  const router = useRouter();
  const [error, setError] = useState<string | null>(null);
  const [ready, setReady] = useState(false);
  const {
    register,
    handleSubmit,
    formState: { errors, isSubmitting },
  } = useForm<FormValues>({ defaultValues: { username: "", password: "" } });
  useEffect(() => {
    getCsrf()
      .then(() => setReady(true))
      .catch(() => setError("无法建立安全会话，请确认 Portal API 正常运行。"));
  }, []);
  async function submit(values: FormValues) {
    setError(null);
    const parsed = schema.safeParse(values);
    if (!parsed.success) {
      setError("请输入登录名和密码。");
      return;
    }
    try {
      const result = await login(parsed.data);
      router.replace(
        result.user.password_state === "RESET_REQUIRED"
          ? "/change-password"
          : result.ssh_enrollment?.required && result.ssh_enrollment.setup_path
            ? result.ssh_enrollment.setup_path
            : "/",
      );
    } catch {
      setError("用户名或密码不正确，或账号暂时被锁定。");
    }
  }
  return (
    <div className="auth-page">
      <Card className="auth-panel">
        <div className="auth-brand">
          <span className="local-mode">{ACCESS_MODE_LABEL}</span>
          <h1>H100 管理平台</h1>
          <p>私有管理入口 · 请使用网页账号登录</p>
        </div>
        <form onSubmit={handleSubmit(submit)} noValidate>
          <div className="form-field">
            <label htmlFor="username">登录名</label>
            <Input
              id="username"
              autoComplete="username"
              placeholder="请输入登录名"
              {...register("username")}
            />
            {errors.username ? (
              <div className="form-error">{errors.username.message}</div>
            ) : null}
          </div>
          <div className="form-field">
            <label htmlFor="password">网页密码</label>
            <Input
              id="password"
              type="password"
              autoComplete="current-password"
              {...register("password")}
            />
            {errors.password ? (
              <div className="form-error">{errors.password.message}</div>
            ) : null}
          </div>
          {error ? (
            <div className="error-box" role="alert">
              {error}
            </div>
          ) : null}
          <div className="form-actions">
            <Button
              tone="primary"
              type="submit"
              disabled={!ready || isSubmitting}
            >
              {isSubmitting ? "登录中…" : "登录"}
            </Button>
          </div>
        </form>
        <div className="auth-foot">
          网页密码与 Linux/SSH
          密码分离。当前入口仅绑定已批准的虚拟网络地址；Pilot
          阶段由管理员接受内部 HTTP，未启用 TLS。
          <br />
          <Link href="/setup-password" className="muted">
            已有一次性设置链接？
          </Link>
        </div>
      </Card>
    </div>
  );
}
