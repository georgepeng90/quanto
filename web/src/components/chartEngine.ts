import { init, use } from 'echarts/core'
import { BarChart, HeatmapChart, LineChart } from 'echarts/charts'
import { DataZoomComponent, GridComponent, LegendComponent, TooltipComponent, VisualMapComponent } from 'echarts/components'
import { SVGRenderer } from 'echarts/renderers'

use([LineChart, BarChart, HeatmapChart, DataZoomComponent, GridComponent, LegendComponent, TooltipComponent, VisualMapComponent, SVGRenderer])

export const createChart = (host: HTMLElement) => init(host, undefined, { renderer: 'svg' })
