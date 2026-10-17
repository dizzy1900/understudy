import "./overlay.css";

import Briefing from "./Briefing";
import Captions from "./Captions";
import Controls from "./Controls";
import Header from "./Header";
import Intro from "./Intro";
import Ladder from "./Ladder";
import ReplayBanner from "./ReplayBanner";
import SkillCardPanel from "./SkillCardPanel";
import Stats from "./Stats";
import { useHotkeys } from "./useHotkeys";
import { ListeningRing } from "./VoicePanel";

export default function Overlay() {
  useHotkeys();

  return (
    <div className="overlay-layer">
      <div className="top-bar interactive">
        <Header />
      </div>
      <aside className="side-left interactive">
        <ReplayBanner />
        <Briefing />
        <Controls />
      </aside>
      <div className="scene-slot">
        <Captions />
      </div>
      <aside className="side-right interactive">
        <Stats />
        <Ladder />
      </aside>
      <SkillCardPanel />
      <ListeningRing />
      <Intro />
    </div>
  );
}
