import { SlashCommandBuilder } from 'discord.js';

export const command = new SlashCommandBuilder()
  .setName('haru')
  .setDescription('이 Mac의 Haru와 한국어 음성 대화')
  .addSubcommand(sub => sub.setName('join').setDescription('내 음성 채널에서 내 목소리로 Haru와 대화 시작'))
  .addSubcommand(sub => sub.setName('leave').setDescription('음성 대화를 종료하고 채널에서 나가기'))
  .addSubcommand(sub => sub.setName('reset').setDescription('대화 기억을 지우고 같은 채널에서 새로 시작'))
  .addSubcommand(sub => sub.setName('status').setDescription('연결·음성 수신·버퍼 상태 확인'));
