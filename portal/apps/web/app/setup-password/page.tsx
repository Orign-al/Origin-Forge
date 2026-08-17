"use client";

import { useQueryClient } from "@tanstack/react-query";
import { useRouter } from "next/navigation";
import { useEffect, useRef, useState } from "react";
import { useForm } from "react-hook-form";
import { z } from "zod";

import { Button, Card, Input } from "@h100-portal/ui";
import {
  ApiError,
  exchangePasswordAction,
  getCsrf,
  setupPassword,
} from "../../lib/api";

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

type PasswordAction = {
  purpose: "INITIAL_PASSWORD_SETUP" | "PASSWORD_RESET";
  username: string;
  display_name: string;
  expires_at: string;
};

type LinkState = "LOADING" | "READY" | "EXPIRED" | "USED" | "INVALID";

function localTime(value: string) {
  return new Intl.DateTimeFormat("zh-CN", {
    timeZone: "Asia/Shanghai",
    dateStyle: "medium",
    timeStyle: "short",
  }).format(new Date(value));
}

export default function SetupPasswordPage() {
  const router = useRouter();
  const queryClient = useQueryClient();
  const [action, setAction] = useState<PasswordAction | null>(null);
  const [linkState, setLinkState] = useState<LinkState>("LOADING");
  const [error, setError] = useState<string | null>(null);
  const [resetComplete, setResetComplete] = useState(false);
  const exchangeStarted = useRef(false);
  const {
    register,
    handleSubmit,
    formState: { errors, isSubmitting },
  } = useForm<FormValues>();

  useEffect(() => {
    if (exchangeStarted.current) return;
    exchangeStarted.current = true;
    const fragment = new URLSearchParams(window.location.hash.slice(1));
    const token = fragment.get("token") ?? "";
    window.history.replaceState(null, "", window.location.pathname);
    if (!token) {
      // Keep all effect-driven state transitions asynchronous. This also
      // avoids treating the server render's token-less URL as a client state.
      void Promise.resolve().then(() => setLinkState("INVALID"));
      return;
    }
    void getCsrf()
      .then(() => exchangePasswordAction(token))
      .then((result) => {
        setAction(result);
        setLinkState("READY");
      })
      .catch((reason: unknown) => {
        if (reason instanceof ApiError) {
          if (reason.code === "PASSWORD_ACTION_EXPIRED") {
            setLinkState("EXPIRED");
            return;
          }
          if (reason.code === "PASSWORD_ACTION_USED") {
            setLinkState("USED");
            return;
          }
        }
        setLinkState("INVALID");
      });
  }, []);

  async function submit(values: FormValues) {
    setError(null);
    const parsed = schema.safeParse(values);
    if (!parsed.success) {
      setError(parsed.error.issues[0]?.message ?? "密码不符合要求");
      return;
    }
    try {
      const result = await setupPassword(parsed.data);
      queryClient.clear();
      if (result.requires_login) {
        setResetComplete(true);
        return;
      }
      router.replace("/");
    } catch (reason) {
      setError(
        reason instanceof ApiError
          ? reason.message
          : "密码设置失败，请重新获取一次性链接。",
      );
    }
  }

  const isReset = action?.purpose === "PASSWORD_RESET";
  return (
    <div className="auth-page">
      <Card className="auth-panel">
        {linkState === "LOADING" ? (
          <div className="ui-empty">正在验证一次性链接…</div>
        ) : linkState !== "READY" ? (
          <div className="auth-brand">
            <span className="local-mode">一次性密码链接</span>
            <h1>
              {linkState === "EXPIRED"
                ? "链接已过期"
                : linkState === "USED"
                  ? "链接已使用"
                  : "链接无效"}
            </h1>
            <p>请联系管理员生成新的密码设置或重置链接。</p>
          </div>
        ) : resetComplete ? (
          <>
            <div className="auth-brand">
              <span className="local-mode">密码重置完成</span>
              <h1>使用新密码登录</h1>
              <p>该账号的旧 Portal 会话已全部撤销，计算资源没有变化。</p>
            </div>
            <Button tone="primary" onClick={() => router.replace("/login")}>
              返回登录
            </Button>
          </>
        ) : (
          <>
            <div className="auth-brand">
              <span className="local-mode">
                {isReset ? "密码重置" : "账号邀请"}
              </span>
              <h1>{isReset ? "重置你的登录密码" : "设置你的登录密码"}</h1>
              <p>此链接只能使用一次。</p>
            </div>
            <dl className="kv-grid compact-action-identity">
              <div className="kv">
                <dt>Username</dt>
                <dd>{action?.username}</dd>
              </div>
              <div className="kv">
                <dt>有效至</dt>
                <dd>{action ? localTime(action.expires_at) : "—"}</dd>
              </div>
            </dl>
            <form onSubmit={handleSubmit(submit)} noValidate>
              <div className="form-field">
                <label htmlFor="password">新密码</label>
                <Input
                  id="password"
                  type="password"
                  autoComplete="new-password"
                  {...register("password")}
                />
                <div className="muted compact-help">
                  14–128 个字符，可使用中文和 Unicode。
                </div>
                {errors.password ? (
                  <div className="form-error">{errors.password.message}</div>
                ) : null}
              </div>
              <div className="form-field">
                <label htmlFor="confirmation">确认新密码</label>
                <Input
                  id="confirmation"
                  type="password"
                  autoComplete="new-password"
                  {...register("confirmation")}
                />
                {errors.confirmation ? (
                  <div className="form-error">
                    {errors.confirmation.message}
                  </div>
                ) : null}
              </div>
              {error ? (
                <div className="error-box" role="alert">
                  {error}
                </div>
              ) : null}
              <div className="form-actions">
                <Button tone="primary" type="submit" disabled={isSubmitting}>
                  {isSubmitting ? "保存中…" : "设置密码并继续"}
                </Button>
              </div>
            </form>
            <div className="auth-foot">
              该操作只修改 Portal 身份密码，不修改 Linux、SSH、Container、Lease
              或其他计算资源。
            </div>
          </>
        )}
      </Card>
    </div>
  );
}
