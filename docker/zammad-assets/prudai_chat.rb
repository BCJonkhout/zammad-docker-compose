# Zammad 7.0 has no per-tag notification matrix. Keep this boundary at the
# actual mail entry points so it covers every agent, owner and subscriber.
module PrudaiChat
  TAG = 'prudai-chat'.freeze

  def self.ticket?(ticket)
    ticket && ticket.tag_list.include?(TAG)
  end

  def self.exclude(condition)
    exclusion = { 'name' => 'ticket.tags', 'operator' => 'contains one not', 'value' => TAG }
    normalized = if condition.key?('conditions')
                   condition
                 else
                   { 'operator' => 'AND', 'conditions' => condition.map { |name, rule| rule.merge('name' => name) } }
                 end
    return normalized if normalized['operator'] == 'AND' && normalized['conditions'].include?(exclusion)

    { 'operator' => 'AND', 'conditions' => [normalized, exclusion] }
  end

  def self.reply_condition(agent_sender_id:, system_sender_id:, email_type_id:, excluded_user_ids:)
    {
      'operator' => 'AND',
      'conditions' => [
        { 'name' => 'ticket.tags', 'operator' => 'contains all', 'value' => TAG },
        { 'name' => 'article.action', 'operator' => 'is', 'value' => 'create' },
        { 'name' => 'article.internal', 'operator' => 'is', 'value' => false },
        { 'name' => 'article.sender_id', 'operator' => 'is not', 'value' => system_sender_id },
        {
          'operator' => 'OR',
          'conditions' => [
            { 'name' => 'article.from', 'operator' => 'is', 'value' => nil },
            { 'name' => 'article.from', 'operator' => 'does not match regex',
              'value' => '(?i)(^\s*|<\s*)(support|no-reply)@prudai[.]com\s*(>|$)' }
          ]
        },
        {
          'operator' => 'OR',
          'conditions' => [
            {
              'operator' => 'AND',
              'conditions' => [
                { 'name' => 'article.sender_id', 'operator' => 'is', 'value' => agent_sender_id },
                { 'name' => 'article.created_by_id', 'operator' => 'is not', 'value' => excluded_user_ids }
              ]
            },
            {
              'operator' => 'AND',
              'conditions' => [
                { 'name' => 'article.type_id', 'operator' => 'is', 'value' => email_type_id },
                { 'name' => 'article.from', 'operator' => 'matches regex',
                  'value' => %q{(?i)^(?:[^<>]*<)?(?!support@|no-reply@)[a-z0-9.!#$%&'*+/=?^_`{|}~-]+@prudai[.]com>?$} }
              ]
            }
          ]
        }
      ]
    }
  end

  module AgentEmail
    private

    def send_to_single_recipient_email(user, ticket, article, changes)
      return if PrudaiChat.ticket?(ticket)

      super
    end
  end

  module AutomaticEmail
    def execute(...)
      return if PrudaiChat.ticket?(record)

      super
    end
  end
end

Rails.application.config.to_prepare do
  Transaction::Notification.prepend(PrudaiChat::AgentEmail) unless Transaction::Notification < PrudaiChat::AgentEmail
  email_action = Ticket::PerformChanges::Action::NotificationEmail
  email_action.prepend(PrudaiChat::AutomaticEmail) unless email_action < PrudaiChat::AutomaticEmail
end
