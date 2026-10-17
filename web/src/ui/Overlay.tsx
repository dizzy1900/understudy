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

export default function Overlay() {
  useHotkeys();

  return (
    <div className="overlay-layer">
      <div className="top-bar">
        <Header />
        <Stats />
      </div>
      <div className="left-stack">
        <ReplayBanner />
        <Briefing />
      </div>
      <Ladder />
      <SkillCardPanel />
      <Captions />
      <Controls />
      <Intro />
    </div>
  );
}
