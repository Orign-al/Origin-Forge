"use client";

import { useQueryClient } from "@tanstack/react-query";
import { useRouter } from "next/navigation";
import { type FormEvent, useEffect, useState } from "react";

import { Button, Card, Input } from "@h100-portal/ui";
import { changePassword, me } from "../../lib/api";

export default function RequiredPasswordChangePage() {
  const router = useRouter();
  const queryClient = useQueryClient();
  const [currentPassword, setCurrentPassword] = useState("");
  const [newPassword, setNewPassword] = useState("");
  const [confirmation, setConfirmation] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  useEffect(() => {
    me()
      .then((result) => {
        if (result.user.password_state !== "RESET_REQUIRED")
          router.replace("/");
      })
      .catch(() => router.replace("/login"));
  }, [router]);
  async function submit(event: FormEvent) {
    event.preventDefault();
    setError(null);
    if (newPassword.length < 14 || newPassword !== confirmation) {
      setError("新密码至少14个字符，且两次输入必须一致。");
      return;
    }
    setBusy(true);
    try {
      await changePassword({
        current_password: currentPassword,
        new_password: newPassword,
        confirmation,
      });
      queryClient.clear();
      router.replace("/");
    } catch {
      setError(
        "密码修改失败。请确认临时密码正确，并使用至少14个字符的新密码。",
      );
    } finally {
      setBusy(false);
    }
  }
  return (
    <div className="auth-page">
      <Card className="auth-panel">
        <div className="auth-brand">
          <h1>首次登录修改密码</h1>
          <p>设置自己的Portal密码后才能进入计算环境。</p>
        </div>
        <form onSubmit={submit}>
          <div className="form-field">
            <label htmlFor="temporary-password">临时密码</label>
            <Input
              id="temporary-password"
              type="password"
              autoComplete="current-password"
              value={currentPassword}
              onChange={(event) => setCurrentPassword(event.target.value)}
            />
          </div>
          <div className="form-field">
            <label htmlFor="new-password">新Portal密码</label>
            <Input
              id="new-password"
              type="password"
              autoComplete="new-password"
              value={newPassword}
              onChange={(event) => setNewPassword(event.target.value)}
            />
          </div>
          <div className="form-field">
            <label htmlFor="confirmation">再次输入</label>
            <Input
              id="confirmation"
              type="password"
              autoComplete="new-password"
              value={confirmation}
              onChange={(event) => setConfirmation(event.target.value)}
            />
          </div>
          {error ? (
            <div className="error-box" role="alert">
              {error}
            </div>
          ) : null}
          <div className="form-actions">
            <Button tone="primary" type="submit" disabled={busy}>
              保存并进入我的环境
            </Button>
          </div>
        </form>
        <div className="auth-foot">
          此操作只修改Portal密码，不设置或解锁Linux密码。
        </div>
      </Card>
    </div>
  );
}
