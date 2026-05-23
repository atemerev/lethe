use std::collections::VecDeque;
use std::time::{Duration, SystemTime, UNIX_EPOCH};

use serde::{Deserialize, Serialize};

use crate::config::Settings;

const DAY_SECONDS: u64 = 86_400;

#[derive(Clone, Debug, Eq, PartialEq, Serialize, Deserialize)]
pub struct ProactiveRateLimiter {
    pub max_per_day: u32,
    pub cooldown_seconds: u64,
    sends: VecDeque<u64>,
}

impl ProactiveRateLimiter {
    pub fn new(max_per_day: u32, cooldown_seconds: u64) -> Self {
        Self {
            max_per_day,
            cooldown_seconds,
            sends: VecDeque::new(),
        }
    }

    pub fn from_settings(settings: &Settings) -> Self {
        Self::new(
            settings.proactive_max_per_day,
            u64::from(settings.proactive_cooldown_minutes) * 60,
        )
    }

    pub fn allowed(&mut self) -> bool {
        self.allowed_at(epoch_seconds())
    }

    pub fn record(&mut self) {
        self.record_at(epoch_seconds());
    }

    pub fn allowed_at(&mut self, now: u64) -> bool {
        self.prune(now);

        if self.max_per_day > 0 && self.sends.len() >= self.max_per_day as usize {
            return false;
        }

        if let Some(last) = self.sends.back()
            && now.saturating_sub(*last) < self.cooldown_seconds
        {
            return false;
        }

        true
    }

    pub fn record_at(&mut self, now: u64) {
        self.prune(now);
        self.sends.push_back(now);
    }

    pub fn send_count(&self) -> usize {
        self.sends.len()
    }

    fn prune(&mut self, now: u64) {
        while let Some(first) = self.sends.front() {
            if now.saturating_sub(*first) > DAY_SECONDS {
                self.sends.pop_front();
            } else {
                break;
            }
        }
    }
}

#[derive(Clone, Debug, Eq, PartialEq, Serialize, Deserialize)]
pub struct ActiveReminder {
    pub title: String,
    pub priority: String,
    pub due: Option<String>,
}

pub fn format_active_reminders(reminders: &[ActiveReminder], limit: usize) -> String {
    reminders
        .iter()
        .take(limit)
        .map(|todo| {
            let due = todo
                .due
                .as_deref()
                .filter(|value| !value.trim().is_empty())
                .map(|value| format!(" (due: {value})"))
                .unwrap_or_default();
            format!("- [{}] {}{}", todo.priority, todo.title, due)
        })
        .collect::<Vec<_>>()
        .join("\n")
}

fn epoch_seconds() -> u64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or(Duration::ZERO)
        .as_secs()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn rate_limiter_enforces_cooldown_and_daily_limit() {
        let mut limiter = ProactiveRateLimiter::new(2, 60);
        assert!(limiter.allowed_at(1_000));
        limiter.record_at(1_000);
        assert!(!limiter.allowed_at(1_030));
        assert!(limiter.allowed_at(1_061));
        limiter.record_at(1_061);
        assert!(!limiter.allowed_at(1_200));
        assert!(limiter.allowed_at(1_000 + DAY_SECONDS + 1));
    }

    #[test]
    fn zero_daily_limit_preserves_unlimited_semantics() {
        let mut limiter = ProactiveRateLimiter::new(0, 0);
        for i in 0..10 {
            assert!(limiter.allowed_at(i));
            limiter.record_at(i);
        }
    }

    #[test]
    fn active_reminders_format_matches_prompt_ready_shape() {
        let reminders = vec![
            ActiveReminder {
                title: "Submit report".to_string(),
                priority: "high".to_string(),
                due: Some("2026-05-23".to_string()),
            },
            ActiveReminder {
                title: "Archive notes".to_string(),
                priority: "normal".to_string(),
                due: None,
            },
        ];

        assert_eq!(
            format_active_reminders(&reminders, 10),
            "- [high] Submit report (due: 2026-05-23)\n- [normal] Archive notes"
        );
    }
}
