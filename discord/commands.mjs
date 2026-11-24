import { SlashCommandBuilder } from 'discord.js';

export const command = new SlashCommandBuilder()
  .setName('haru')
  .setDescription('하루와 한국어 음성 및 채팅 대화')
  .addSubcommand(sub => sub.setName('join').setDescription('내 음성 채널의 참가자들과 Haru 대화 시작'))
  .addSubcommand(sub => sub.setName('leave').setDescription('음성 대화를 종료하고 채널에서 나가기'))
  .addSubcommand(sub => sub.setName('reset').setDescription('대화 기억을 지우고 같은 채널에서 새로 시작'))
  .addSubcommand(sub => sub.setName('status').setDescription('연결·음성 수신·버퍼 상태 확인'))
  .addSubcommandGroup(group => group.setName('chat').setDescription('호출어 채팅 채널 설정')
    .addSubcommand(sub => sub.setName('add').setDescription('글로 부르면 통화방에서 음성으로 답할 채팅 채널 선택'))
    .addSubcommand(sub => sub.setName('list').setDescription('하루가 호출을 기다리는 채널 목록'))
    .addSubcommand(sub => sub.setName('remove').setDescription('호출어 채팅을 끌 채널을 하나 이상 선택')));
