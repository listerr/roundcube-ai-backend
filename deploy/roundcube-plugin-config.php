<?php
// plugins/ai_agent/config.inc.php in Roundcube.
// The chat URL is <api_url>v1/ when ai_agent_chat_url is null.
// To use a random sub-URL instead of /rcapi, read docs/install.md, section 7.
$config['ai_agent_api_url'] = 'https://webmail.example.com/rcapi/';
$config['ai_agent_chat_url'] = null;
// The backend ignores this name and uses [openai] model from its own config.
$config['ai_agent_model'] = 'mail-ai';
