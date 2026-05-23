use std::cmp::Reverse;
use std::collections::HashMap;

use chrono::{Duration as ChronoDuration, Utc};
use serde_json::{Value, json};

use super::helpers::*;
use super::*;

#[derive(Debug)]
pub struct ActorRegistry {
    pub(super) actors: HashMap<String, Actor>,
    principal_id: Option<String>,
    pub events: ActorEventBus,
}

impl ActorRegistry {
    pub const STALE_SECONDS: i64 = 60 * 60;

    pub fn new() -> Self {
        Self {
            actors: HashMap::new(),
            principal_id: None,
            events: ActorEventBus::new(1000),
        }
    }

    pub fn spawn(
        &mut self,
        config: ActorConfig,
        spawned_by: Option<&str>,
        is_principal: bool,
    ) -> String {
        let mut actor = Actor::new(config, spawned_by, is_principal);
        actor.state = ActorState::Running;
        actor.task_state = TaskState::Running;
        let actor_id = actor.id.clone();
        if is_principal {
            self.principal_id = Some(actor_id.clone());
        }
        self.emit_event(
            "actor_spawned",
            &actor,
            json!({
                "name": actor.config.name,
                "spawned_by": actor.spawned_by,
                "is_principal": is_principal,
            }),
        );
        self.actors.insert(actor_id.clone(), actor);
        actor_id
    }

    pub fn get(&self, actor_id: &str) -> Option<&Actor> {
        self.actors.get(actor_id)
    }

    pub fn get_mut(&mut self, actor_id: &str) -> Option<&mut Actor> {
        self.actors.get_mut(actor_id)
    }

    pub fn get_principal(&self) -> Option<&Actor> {
        self.principal_id
            .as_deref()
            .and_then(|actor_id| self.actors.get(actor_id))
    }

    pub fn active_count(&self) -> usize {
        self.actors
            .values()
            .filter(|actor| actor.state != ActorState::Terminated)
            .count()
    }

    pub fn all_actors(&self) -> Vec<ActorInfo> {
        self.actors.values().map(Actor::info).collect()
    }

    pub fn discover(&self, group: &str) -> Vec<ActorInfo> {
        self.actors
            .values()
            .filter(|actor| actor.config.group == group)
            .map(Actor::info)
            .collect()
    }

    pub fn discover_active(&self, group: &str) -> Vec<ActorInfo> {
        self.actors
            .values()
            .filter(|actor| actor.config.group == group && actor.state != ActorState::Terminated)
            .map(Actor::info)
            .collect()
    }

    pub fn discover_terminated(&self, group: &str) -> Vec<ActorInfo> {
        self.actors
            .values()
            .filter(|actor| actor.config.group == group && actor.state == ActorState::Terminated)
            .map(Actor::info)
            .collect()
    }

    pub fn discover_recently_finished(&self, group: &str, limit: usize) -> Vec<Actor> {
        let mut actors = self
            .actors
            .values()
            .filter(|actor| actor.config.group == group && actor.state == ActorState::Terminated)
            .cloned()
            .collect::<Vec<_>>();
        actors.sort_by_key(|actor| Reverse(actor.terminated_at));
        actors.truncate(limit.max(1));
        actors
    }

    pub fn find_by_name(&self, name: &str, group: Option<&str>) -> Option<&Actor> {
        self.actors.values().find(|actor| {
            actor.config.name == name
                && actor.state != ActorState::Terminated
                && group.is_none_or(|group| actor.config.group == group)
        })
    }

    pub fn get_children(&self, parent_id: &str) -> Vec<&Actor> {
        self.actors
            .values()
            .filter(|actor| actor.spawned_by == parent_id)
            .collect()
    }

    pub fn can_message(&self, sender_id: &str, recipient_id: &str) -> bool {
        let Some(sender) = self.actors.get(sender_id) else {
            return false;
        };
        let Some(recipient) = self.actors.get(recipient_id) else {
            return false;
        };
        recipient_id == sender.spawned_by
            || recipient.spawned_by == sender.id
            || (!sender.spawned_by.is_empty() && recipient.spawned_by == sender.spawned_by)
            || recipient.config.group == sender.config.group
            || sender.is_principal
    }

    pub fn send_to(
        &mut self,
        sender_id: &str,
        recipient_id: &str,
        content: impl Into<String>,
        reply_to: Option<String>,
        mut metadata: serde_json::Map<String, Value>,
        intent: Option<MessageIntent>,
    ) -> ActorResult<ActorMessage> {
        let sender = self
            .actors
            .get(sender_id)
            .ok_or_else(|| ActorError::NotFound(sender_id.to_string()))?
            .clone();
        if !self.actors.contains_key(recipient_id) {
            return Err(ActorError::NotFound(recipient_id.to_string()));
        }
        if !self.can_message(sender_id, recipient_id) {
            return Err(ActorError::PermissionDenied {
                sender: sender_id.to_string(),
                recipient: recipient_id.to_string(),
            });
        }

        let resolved_intent = intent.unwrap_or_else(|| {
            MessageIntent::from_strings(
                metadata
                    .get("channel")
                    .and_then(Value::as_str)
                    .unwrap_or_default(),
                metadata
                    .get("kind")
                    .and_then(Value::as_str)
                    .unwrap_or_default(),
            )
        });
        metadata
            .entry("channel".to_string())
            .or_insert_with(|| json!(resolved_intent.channel()));
        metadata
            .entry("kind".to_string())
            .or_insert_with(|| json!(intent_name(resolved_intent)));

        let message = ActorMessage {
            id: short_id(),
            sender: sender_id.to_string(),
            recipient: recipient_id.to_string(),
            content: content.into(),
            reply_to,
            intent: resolved_intent,
            metadata,
            created_at: Utc::now(),
        };

        if let Some(recipient) = self.actors.get_mut(recipient_id) {
            recipient.messages.push(message.clone());
            recipient.inbox.push_back(message.clone());
        }
        if let Some(sender) = self.actors.get_mut(sender_id) {
            sender.messages.push(message.clone());
        }

        self.emit_event(
            "actor_message",
            &sender,
            json!({
                "recipient": recipient_id,
                "message_id": message.id,
                "content_preview": message.content.chars().take(200).collect::<String>(),
                "intent": intent_name(resolved_intent),
                "channel": resolved_intent.channel(),
            }),
        );
        if resolved_intent.channel() == "user_notify" {
            self.emit_event(
                "user_notify",
                &sender,
                json!({
                    "recipient": recipient_id,
                    "message_id": message.id,
                    "message": message.content.trim(),
                    "channel": resolved_intent.channel(),
                    "kind": intent_name(resolved_intent),
                    "metadata": message.metadata,
                }),
            );
        }

        Ok(message)
    }

    pub fn pop_inbox(&mut self, actor_id: &str) -> Option<ActorMessage> {
        self.actors
            .get_mut(actor_id)
            .and_then(|actor| actor.inbox.pop_front())
    }

    pub fn set_task_state(
        &mut self,
        actor_id: &str,
        state: &str,
        note: impl Into<String>,
    ) -> ActorResult<String> {
        let new_state = parse_task_state(state)
            .ok_or_else(|| ActorError::InvalidTaskState(state.to_string()))?;
        let actor = self
            .actors
            .get(actor_id)
            .ok_or_else(|| ActorError::NotFound(actor_id.to_string()))?
            .clone();
        if !valid_task_transition(actor.task_state, new_state) {
            return Err(ActorError::InvalidTaskTransition {
                from: actor.task_state,
                to: new_state,
            });
        }

        let note = note.into();
        let updated = self
            .actors
            .get_mut(actor_id)
            .ok_or_else(|| ActorError::NotFound(actor_id.to_string()))?;
        let previous = updated.task_state;
        updated.task_state = new_state;
        updated.task_state_note = note.trim().to_string();
        updated.task_state_updated_at = Some(Utc::now());
        let snapshot = updated.clone();

        self.emit_event(
            "task_state_changed",
            &snapshot,
            json!({
                "from": state_name(previous),
                "to": state_name(new_state),
                "note": snapshot.task_state_note,
            }),
        );
        Ok(format!(
            "Task state updated: {} -> {}",
            state_name(previous),
            state_name(new_state)
        ))
    }

    pub fn terminate(&mut self, actor_id: &str, result: impl Into<String>) -> ActorResult<bool> {
        let result = result.into();
        let actor = self
            .actors
            .get(actor_id)
            .ok_or_else(|| ActorError::NotFound(actor_id.to_string()))?
            .clone();
        if actor.state == ActorState::Terminated {
            return Ok(false);
        }

        let result_text = if result.trim().is_empty() {
            format!("Actor {} terminated", actor.config.name)
        } else {
            result
        };
        {
            let actor = self
                .actors
                .get_mut(actor_id)
                .ok_or_else(|| ActorError::NotFound(actor_id.to_string()))?;
            actor.result = Some(result_text.clone());
            actor.task_state = TaskState::Done;
            actor.state = ActorState::Terminated;
            actor.terminated_at = Some(Utc::now());
        }

        self.notify_parent_on_termination(actor_id)?;
        let actor = self
            .actors
            .get(actor_id)
            .ok_or_else(|| ActorError::NotFound(actor_id.to_string()))?
            .clone();
        self.emit_event(
            "actor_terminated",
            &actor,
            json!({
                "name": actor.config.name,
                "result": actor.result.clone().unwrap_or_default(),
                "turns": actor.turn_count,
            }),
        );
        Ok(true)
    }

    pub fn finish_turn_or_terminate(
        &mut self,
        actor_id: &str,
        result: impl Into<String>,
    ) -> ActorResult<bool> {
        let actor = self
            .actors
            .get(actor_id)
            .ok_or_else(|| ActorError::NotFound(actor_id.to_string()))?
            .clone();
        if !actor.config.persistent {
            return self.terminate(actor_id, result);
        }
        if actor.state == ActorState::Terminated {
            return Ok(false);
        }

        let result = result.into();
        let result_text = if result.trim().is_empty() {
            format!("Actor {} completed a cycle", actor.config.name)
        } else {
            result
        };
        {
            let actor = self
                .actors
                .get_mut(actor_id)
                .ok_or_else(|| ActorError::NotFound(actor_id.to_string()))?;
            actor.result = Some(result_text.clone());
            actor.task_state = TaskState::Running;
            actor.state = ActorState::Waiting;
        }
        let actor = self
            .actors
            .get(actor_id)
            .ok_or_else(|| ActorError::NotFound(actor_id.to_string()))?
            .clone();
        self.emit_event(
            "actor_cycle_finished",
            &actor,
            json!({
                "name": actor.config.name,
                "result": result_text,
                "turns": actor.turn_count,
            }),
        );
        Ok(false)
    }

    pub fn kill_child(&mut self, parent_id: &str, child_id: &str) -> ActorResult<bool> {
        let parent = self
            .actors
            .get(parent_id)
            .ok_or_else(|| ActorError::NotFound(parent_id.to_string()))?
            .clone();
        let child = self
            .actors
            .get(child_id)
            .ok_or_else(|| ActorError::NotFound(child_id.to_string()))?
            .clone();
        if child.spawned_by != parent_id || child.state == ActorState::Terminated {
            return Ok(false);
        }
        self.terminate(child_id, format!("Killed by parent {}", parent.config.name))
    }

    pub fn cleanup_terminated(&mut self, force: bool) -> usize {
        let now = Utc::now();
        let stale = self
            .actors
            .iter()
            .filter_map(|(actor_id, actor)| {
                if actor.state != ActorState::Terminated {
                    return None;
                }
                if force
                    || actor.terminated_at.is_some_and(|terminated_at| {
                        now.signed_duration_since(terminated_at)
                            > ChronoDuration::seconds(Self::STALE_SECONDS)
                    })
                {
                    Some(actor_id.clone())
                } else {
                    None
                }
            })
            .collect::<Vec<_>>();
        let removed = stale.len();
        for actor_id in stale {
            self.actors.remove(&actor_id);
            if self.principal_id.as_deref() == Some(&actor_id) {
                self.principal_id = None;
            }
        }
        removed
    }

    pub(super) fn prepare_actor_turn(
        &mut self,
        actor_id: &str,
    ) -> ActorResult<Option<ActorRunSpec>> {
        let actor = self
            .actors
            .get(actor_id)
            .ok_or_else(|| ActorError::NotFound(actor_id.to_string()))?
            .clone();
        if actor.is_principal || actor.state == ActorState::Terminated {
            return Ok(None);
        }
        if actor.turn_count >= actor.config.max_turns.max(1) {
            self.terminate(
                actor_id,
                format!(
                    "Max turns reached ({}) before completing task.",
                    actor.config.max_turns.max(1)
                ),
            )?;
            return Ok(None);
        }

        let system_prompt = self.build_system_prompt(actor_id)?;
        let updated = self
            .actors
            .get_mut(actor_id)
            .ok_or_else(|| ActorError::NotFound(actor_id.to_string()))?;
        let has_pending_messages = updated.has_pending_messages();
        updated.inbox.clear();
        updated.turn_count += 1;
        updated.state = ActorState::Running;
        let spec = ActorRunSpec {
            actor_id: updated.id.clone(),
            name: updated.config.name.clone(),
            system_prompt,
            turn_number: updated.turn_count,
            max_turns: updated.config.max_turns.max(1),
            model: updated.config.model.unwrap_or(ModelTier::Aux),
            has_pending_messages,
            requested_tools: updated.config.tools.clone(),
        };
        Ok(Some(spec))
    }

    pub(super) fn record_actor_turn_response(
        &mut self,
        actor_id: &str,
        response: impl Into<String>,
    ) -> ActorResult<()> {
        let actor = self
            .actors
            .get_mut(actor_id)
            .ok_or_else(|| ActorError::NotFound(actor_id.to_string()))?;
        if actor.state == ActorState::Terminated {
            return Ok(());
        }
        let response = response.into();
        if !response.trim().is_empty() {
            actor.messages.push(ActorMessage {
                id: short_id(),
                sender: actor_id.to_string(),
                recipient: actor_id.to_string(),
                content: response,
                reply_to: None,
                intent: MessageIntent::Info,
                metadata: serde_json::Map::new(),
                created_at: Utc::now(),
            });
        }
        actor.state = ActorState::Waiting;
        Ok(())
    }

    pub fn should_autocontinue_actor(&self, actor_id: &str) -> bool {
        let Some(actor) = self.actors.get(actor_id) else {
            return false;
        };
        if actor.is_principal
            || actor.state == ActorState::Terminated
            || matches!(actor.task_state, TaskState::Blocked | TaskState::Done)
            || actor.turn_count >= actor.config.max_turns.max(1)
        {
            return false;
        }
        if actor.config.persistent {
            actor.has_pending_messages()
        } else {
            true
        }
    }

    pub fn build_system_prompt(&self, actor_id: &str) -> ActorResult<String> {
        let actor = self
            .actors
            .get(actor_id)
            .ok_or_else(|| ActorError::NotFound(actor_id.to_string()))?;
        let mut parts = Vec::new();
        if actor.is_principal {
            parts.push(
                "You are the cortex - the conscious executive layer. Handle quick tasks directly and spawn subagents for longer work. Use request_tool for extended tools. Actor tools include spawn_actor, spawn_chain, send_message, discover_actors, discover_recently_finished, ping_actor, kill_actor, and update_task_state."
                    .to_string(),
            );
        } else {
            let parent_name = self
                .actors
                .get(&actor.spawned_by)
                .map(|parent| parent.config.name.as_str())
                .unwrap_or(actor.spawned_by.as_str());
            parts.push(format!(
                "You are a subagent actor named '{}'. You were spawned by '{}' (id={}) and cannot talk to the user directly.",
                actor.config.name, parent_name, actor.spawned_by
            ));
        }
        parts.push(format!("\n<goals>\n{}\n</goals>", actor.config.goals));

        let mut visible = self.discover_active(&actor.config.group);
        visible.retain(|info| info.id != actor.id);
        visible.sort_by(|left, right| left.name.cmp(&right.name).then(left.id.cmp(&right.id)));
        let children = self.get_children(&actor.id);
        for child in children {
            if child.state != ActorState::Terminated
                && !visible.iter().any(|info| info.id == child.id)
            {
                visible.push(child.info());
            }
        }
        if !visible.is_empty() {
            parts.push("\n<visible_actors>".to_string());
            for info in visible
                .into_iter()
                .take(if actor.is_principal { 10 } else { 6 })
            {
                let relationship = if info.spawned_by == actor.id {
                    " [child]"
                } else if info.id == actor.spawned_by {
                    " [parent]"
                } else if !actor.spawned_by.is_empty() && info.spawned_by == actor.spawned_by {
                    " [sibling]"
                } else {
                    ""
                };
                parts.push(format!(
                    "- {} (id={}, state={}, task={}){}: {}",
                    info.name,
                    info.id,
                    actor_state_name(info.state),
                    state_name(info.task_state),
                    relationship,
                    truncate_chars(&info.goals, if actor.is_principal { 180 } else { 160 }),
                ));
            }
            parts.push("</visible_actors>".to_string());
        }

        let inbox_messages = actor
            .messages
            .iter()
            .filter(|message| message.sender != actor.id)
            .rev()
            .take(if actor.is_principal { 8 } else { 5 })
            .cloned()
            .collect::<Vec<_>>()
            .into_iter()
            .rev()
            .collect::<Vec<_>>();
        if !inbox_messages.is_empty() {
            parts.push("\n<inbox_block>".to_string());
            for message in inbox_messages {
                let sender_name = self
                    .actors
                    .get(&message.sender)
                    .map(|sender| sender.config.name.as_str())
                    .unwrap_or(message.sender.as_str());
                parts.push(format!(
                    "<actor_message_block from=\"{}\" timestamp=\"{}\">{}</actor_message_block>",
                    sender_name,
                    message.created_at.format("%a %Y-%m-%d %H:%M:%S UTC"),
                    truncate_chars(&message.content, if actor.is_principal { 280 } else { 220 })
                ));
            }
            parts.push("</inbox_block>".to_string());
        }

        if actor.is_principal {
            parts.push("\n<rules>\nHandle quick tasks directly. Spawn subagents for long or isolated work. Use discover_actors before spawning duplicates.\n</rules>".to_string());
        } else {
            parts.push("\n<rules>\nReport results to your parent before terminating. Use update_task_state for meaningful progress.\n</rules>".to_string());
        }
        Ok(parts.join("\n"))
    }

    pub fn send_message_tool(
        &mut self,
        actor_id: &str,
        target_id: &str,
        content: &str,
        reply_to: Option<String>,
        channel: &str,
        kind: &str,
    ) -> String {
        let Some(target) = self.actors.get(target_id).cloned() else {
            return format!(
                "Error: actor {target_id} not found. Use discover_actors() to find available actors."
            );
        };
        if target.state == ActorState::Terminated {
            return format!(
                "Error: actor {target_id} ({}) is terminated.",
                target.config.name
            );
        }
        if !self.can_message(actor_id, target_id) {
            return format!(
                "Error: cannot message {target_id} - not a parent, sibling, child, or group member."
            );
        }
        let mut metadata = serde_json::Map::new();
        if !channel.trim().is_empty() {
            metadata.insert("channel".to_string(), json!(channel.trim()));
        }
        if !kind.trim().is_empty() {
            metadata.insert("kind".to_string(), json!(kind.trim()));
        }
        match self.send_to(actor_id, target_id, content, reply_to, metadata, None) {
            Ok(message) => format!(
                "Message sent (id={}) to {} ({target_id})",
                message.id, target.config.name
            ),
            Err(error) => format!("Error: {error}"),
        }
    }

    pub fn discover_for_actor(
        &self,
        actor_id: &str,
        group: Option<&str>,
        include_terminated: bool,
    ) -> ActorResult<String> {
        let actor = self
            .actors
            .get(actor_id)
            .ok_or_else(|| ActorError::NotFound(actor_id.to_string()))?;
        let search_group = group
            .map(str::trim)
            .filter(|group| !group.is_empty())
            .unwrap_or(&actor.config.group);
        let actors = if include_terminated {
            self.discover(search_group)
        } else {
            self.discover_active(search_group)
        };
        if actors.is_empty() {
            let scope = if include_terminated {
                " (including terminated)"
            } else {
                ""
            };
            return Ok(format!("No actors in group '{search_group}'{scope}."));
        }

        let scope = if include_terminated {
            " (including terminated)"
        } else {
            " (active only)"
        };
        let mut lines = vec![format!("Actors in group '{search_group}'{scope}:")];
        for info in actors {
            let marker = if info.id == actor.id { " (you)" } else { "" };
            let relationship = relationship_label(actor, &info);
            let result_info = if info.state == ActorState::Terminated {
                self.actors
                    .get(&info.id)
                    .and_then(|actor| actor.result())
                    .map(|result| format!(" result: {}", truncate_chars(result, 100)))
                    .unwrap_or_default()
            } else {
                String::new()
            };
            lines.push(format!(
                "  {} (id={}, state={}, task={}){}{}: {}{}",
                info.name,
                info.id,
                actor_state_name(info.state),
                state_name(info.task_state),
                marker,
                relationship,
                info.goals,
                result_info
            ));
        }
        Ok(lines.join("\n"))
    }

    pub fn discover_recently_finished_for_actor(
        &self,
        actor_id: &str,
        group: Option<&str>,
        limit: usize,
    ) -> ActorResult<String> {
        let actor = self
            .actors
            .get(actor_id)
            .ok_or_else(|| ActorError::NotFound(actor_id.to_string()))?;
        let search_group = group
            .map(str::trim)
            .filter(|group| !group.is_empty())
            .unwrap_or(&actor.config.group);
        let finished = self.discover_recently_finished(search_group, limit);
        if finished.is_empty() {
            return Ok(format!(
                "No recently finished actors in group '{search_group}'."
            ));
        }
        let mut lines = vec![format!("Recently finished in '{search_group}':")];
        for actor in finished {
            let result = truncate_chars(actor.result.as_deref().unwrap_or("no result").trim(), 160);
            let when = actor
                .terminated_at
                .map(|value| value.format("%H:%M:%S").to_string())
                .unwrap_or_else(|| "unknown".to_string());
            lines.push(format!(
                "  {} (id={}, task={}, at={}): {}",
                actor.config.name,
                actor.id,
                state_name(actor.task_state),
                when,
                result
            ));
        }
        Ok(lines.join("\n"))
    }

    pub fn spawn_child_for_actor(
        &mut self,
        actor_id: &str,
        request: ActorSpawnRequest<'_>,
    ) -> ActorResult<String> {
        let actor = self
            .actors
            .get(actor_id)
            .ok_or_else(|| ActorError::NotFound(actor_id.to_string()))?
            .clone();
        let target_group = request
            .group
            .map(str::trim)
            .filter(|group| !group.is_empty())
            .unwrap_or(&actor.config.group)
            .to_string();
        let name = normalize_actor_name(request.name);
        if name.is_empty() {
            return Err(ActorError::InvalidActorConfig(
                "actor name is required".to_string(),
            ));
        }
        if request.goals.trim().is_empty() {
            return Err(ActorError::InvalidActorConfig(
                "actor goals are required".to_string(),
            ));
        }

        let active_children = self
            .get_children(&actor.id)
            .into_iter()
            .filter(|child| child.state != ActorState::Terminated)
            .cloned()
            .collect::<Vec<_>>();
        if let Some(existing) = self.find_by_name(&name, Some(&target_group))
            && existing.state != ActorState::Terminated
        {
            return Ok(format!(
                "DUPLICATE BLOCKED: Actor '{name}' already exists (id={}, state={}).\nGoals: {}\nUse send_message({}, ...) to communicate with it, or kill_actor({}) first.{}",
                existing.id,
                actor_state_name(existing.state),
                existing.config.goals,
                existing.id,
                existing.id,
                format_active_children(&active_children)
            ));
        }
        if active_children.len() >= 5 {
            return Ok(format!(
                "TOO MANY CHILDREN: {} active (max 5).\nKill or wait for existing actors before spawning new ones.{}",
                active_children.len(),
                format_active_children(&active_children)
            ));
        }
        if !actor.spawned_by.is_empty()
            && self
                .actors
                .get(&actor.spawned_by)
                .is_some_and(|parent| !parent.spawned_by.is_empty() && !parent.is_principal)
        {
            return Ok(
                "NESTING LIMIT: You are already a grandchild actor (2 levels deep). Cannot spawn further children."
                    .to_string(),
            );
        }

        let model_tier = parse_model_tier(request.model).unwrap_or(ModelTier::Aux);
        let tool_list = split_tool_list(request.tools);
        let mut config =
            ActorConfig::new(name.clone(), request.goals.trim()).in_group(target_group.clone());
        config.tools = tool_list.clone();
        config.model = Some(model_tier);
        config.max_turns = if request.max_turns == 0 {
            20
        } else {
            request.max_turns
        };
        let child_id = self.spawn(config, Some(&actor.id), false);
        let active_children = self
            .get_children(&actor.id)
            .into_iter()
            .filter(|child| child.state != ActorState::Terminated)
            .cloned()
            .collect::<Vec<_>>();
        let tools_text = if tool_list.is_empty() {
            String::new()
        } else {
            format!(" + {}", tool_list.join(", "))
        };
        Ok(format!(
            "Spawned actor '{name}' (id={child_id}, group={target_group}, model={}).\nGoals: {}\nTools: default (bash, file I/O, grep, view_image){tools_text} + actor tools\nIt will work autonomously and message you when done.{}",
            intent_model_name(model_tier),
            truncate_chars(request.goals.trim(), 200),
            format_active_children(&active_children)
        ))
    }

    pub fn kill_actor_tool(&mut self, actor_id: &str, target_id: &str) -> String {
        match self.kill_child(actor_id, target_id) {
            Ok(true) => format!("Killed actor {target_id}."),
            Ok(false) => format!("Cannot kill {target_id}: not your child or already terminated."),
            Err(error) => format!("Error: {error}"),
        }
    }

    pub fn ping_actor(&self, actor_id: &str) -> String {
        let Some(actor) = self.actors.get(actor_id) else {
            return format!("Actor {actor_id} not found.");
        };
        let result = actor
            .result()
            .map(|result| format!("\nResult: {result}"))
            .unwrap_or_default();
        format!(
            "{} (id={}, state={}, task={}): {}{}",
            actor.config.name,
            actor.id,
            actor_state_name(actor.state),
            state_name(actor.task_state),
            actor.config.goals,
            result
        )
    }

    pub fn terminate_tool(
        &mut self,
        actor_id: &str,
        result: &str,
        outcome: &str,
        files_touched: &str,
        follow_up: &str,
    ) -> String {
        let mut parts = Vec::new();
        if !outcome.trim().is_empty() && outcome.trim() != "success" {
            parts.push(format!("[outcome: {}]", outcome.trim()));
        }
        parts.push(if result.trim().is_empty() {
            "Done".to_string()
        } else {
            result.trim().to_string()
        });
        if !files_touched.trim().is_empty() {
            parts.push(format!("[files: {}]", files_touched.trim()));
        }
        if !follow_up.trim().is_empty() {
            parts.push(format!("[follow-up: {}]", follow_up.trim()));
        }
        match self.finish_turn_or_terminate(actor_id, parts.join("\n")) {
            Ok(true) => "Terminated. Result sent to parent.".to_string(),
            Ok(false) => "Cycle finished. Persistent actor remains alive.".to_string(),
            Err(error) => format!("Error: {error}"),
        }
    }

    pub fn restart_self(&mut self, actor_id: &str, new_goals: &str) -> String {
        let result = format!("Restart requested with new goals:\n{}", new_goals.trim());
        match self.terminate(actor_id, result) {
            Ok(_) => "Restart requested. Parent can spawn a replacement with the revised goals."
                .to_string(),
            Err(error) => format!("Error: {error}"),
        }
    }

    fn notify_parent_on_termination(&mut self, actor_id: &str) -> ActorResult<()> {
        let actor = self
            .actors
            .get(actor_id)
            .ok_or_else(|| ActorError::NotFound(actor_id.to_string()))?
            .clone();
        if actor.spawned_by.is_empty() {
            return Ok(());
        }
        let Some(parent) = self.actors.get(&actor.spawned_by).cloned() else {
            return Ok(());
        };
        if parent.state == ActorState::Terminated {
            return Ok(());
        }
        let already_notified = parent
            .messages
            .iter()
            .any(|message| message.sender == actor_id && message.intent.is_terminal());
        if already_notified {
            return Ok(());
        }

        let result = actor.result.as_deref().unwrap_or("no result").trim();
        let failed = is_failed_result(result);
        let intent = if failed {
            MessageIntent::Failed
        } else {
            MessageIntent::Done
        };
        let mut metadata = serde_json::Map::new();
        metadata.insert("channel".to_string(), json!(intent.channel()));
        metadata.insert("kind".to_string(), json!(intent_name(intent)));
        metadata.insert("source".to_string(), json!("termination"));
        let message = ActorMessage {
            id: short_id(),
            sender: actor_id.to_string(),
            recipient: actor.spawned_by.clone(),
            content: format!("{}: {result}", actor.config.name),
            reply_to: None,
            intent,
            metadata,
            created_at: Utc::now(),
        };
        if let Some(parent) = self.actors.get_mut(&actor.spawned_by) {
            parent.messages.push(message.clone());
            parent.inbox.push_back(message.clone());
        }
        self.emit_event(
            "actor_message",
            &actor,
            json!({
                "recipient": actor.spawned_by,
                "message_id": message.id,
                "content_preview": message.content.chars().take(200).collect::<String>(),
                "intent": intent_name(intent),
                "channel": intent.channel(),
            }),
        );
        Ok(())
    }

    fn emit_event(&mut self, event_type: &str, actor: &Actor, payload: Value) {
        let mut event = ActorEvent::new(event_type, &actor.id);
        event.group = actor.config.group.clone();
        event.payload = payload.as_object().cloned().unwrap_or_default();
        self.events.emit(event);
    }
}

impl Default for ActorRegistry {
    fn default() -> Self {
        Self::new()
    }
}
