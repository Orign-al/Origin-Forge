"use client";

import { useRouter, useSearchParams } from "next/navigation";
import { Suspense, useEffect, useState } from "react";
import { useForm } from "react-hook-form";
import { z } from "zod";

import { Button, Card, Input } from "@h100-portal/ui";
import { getCsrf, setupPassword } from "../../lib/api";

const schema = z
  .object({
    password: z
      .string()
      .min(14, "密码至少 14 个字符")
      .max(128, "密码最多 128 个字符"),
    confirmation: z.string().min(14, "请再次输入密码"),
  })
  .refine((value) => value.password === value.confirmation, {
    path: ["confirmation"],
    message: "两次输入的密码不一致",
  });
type FormValues = z.infer<typeof schema>;

function SetupPasswordForm() {
  const router = useRouter();
  const params = useSearchParams();
  const token = params.get("token") ?? "";
  const [error, setError] = useState<string | null>(null);
  const [ready, setReady] = useState(false);
  const {
    register,
    handleSubmit,
    formState: { errors, isSubmitting },
  } = useForm<FormValues>();
  useEffect(() => {
    getCsrf()
      .then(() => setReady(true))
      .catch(() => setError("无法建立安全会话。"));
  }, []);
  async function submit(values: FormValues) {
    setError(null);
    const parsed = schema.safeParse(values);
    if (!parsed.success) {
      setError(parsed.error.issues[0]?.message ?? "密码不符合要求");
      return;
    }
    try {
      await setupPassword({ token, ...parsed.data });
      router.replace("/");
    } catch {
      setError("设置链接无效、已过期或密码不符合安全要求。");
    }
  }
  return (
    <div className="auth-page">
      <Card className="auth-panel">
        <div className="auth-brand">
          <span className="local-mode">一次性邀请</span>
          <h1>设置 Origin-al 网页密码</h1>
          <p>只设置 Portal 密码，不修改 Linux 或 SSH 密码。</p>
        </div>
        {!token ? (
          <div className="error-box">
            缺少一次性设置 token。请使用管理员终端显示的完整链接。
          </div>
        ) : (
          <form onSubmit={handleSubmit(submit)} noValidate>
            <div className="form-field">
              <label htmlFor="password">新网页密码</label>
              <Input
                id="password"
                type="password"
                autoComplete="new-password"
                {...register("password")}
              />
              <div className="muted" style={{ marginTop: 5, fontSize: 12 }}>
                14–128 个字符，可使用中文和 Unicode；不会 trim 前后空格。
              </div>
              {errors.password ? (
                <div className="form-error">{errors.password.message}</div>
              ) : null}
            </div>
            <div className="form-field">
              <label htmlFor="confirmation">再次输入密码</label>
              <Input
                id="confirmation"
                type="password"
                autoComplete="new-password"
                {...register("confirmation")}
              />
              {errors.confirmation ? (
                <div className="form-error">{errors.confirmation.message}</div>
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
                {isSubmitting ? "保存中…" : "设置密码并进入平台"}
              </Button>
            </div>
          </form>
        )}
        <div className="auth-foot">
          链接单次有效，默认 30 分钟过期。不要把 token 转发或写入日志。
        </div>
      </Card>
    </div>
  );
}

export default function SetupPasswordPage() {
  return (
    <Suspense
      fallback={
        <div className="auth-page">
          <Card className="auth-panel">
            <div className="ui-empty">正在验证一次性设置入口…</div>
          </Card>
        </div>
      }
    >
      <SetupPasswordForm />
    </Suspense>
  );
}
